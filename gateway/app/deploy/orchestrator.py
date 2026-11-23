"""Asynchronous deployment orchestration against the Kubernetes API."""

import asyncio
import logging
from typing import Any, Callable

import httpx

from ..domain.helpers import to_model_slug
from ..domain.k8s_labels import OPERATION_ID_ANNOTATION
from ..domain.types import DeployOperationRecord, DeployOperationRequest
from ..kubernetes.client import KubernetesApiClient
from .errors import (
    MAX_ERROR_CHARS,
    DeployStepError,
    build_failure,
    extract_failure_reason,
    truncate,
)
from .operation_store import DeployOperationStore

logger = logging.getLogger("bentoml.gateway.deploy")


def _default_http_client_factory(verify: str | bool) -> httpx.AsyncClient:
    """Create the default HTTP client used for Kubernetes API interactions."""

    return httpx.AsyncClient(timeout=20.0, verify=verify)


class DeployOrchestrator:
    """Executes asynchronous model deploy operations against Kubernetes."""

    def __init__(
        self,
        operation_store: DeployOperationStore,
        k8s_client: KubernetesApiClient,
        resolve_k8s_auth: Callable[[], tuple[str, dict[str, str], str | bool]],
        build_job_manifest: Callable[..., dict[str, Any]],
        build_delete_job_manifest: Callable[..., dict[str, Any]],
        build_deployment_manifest: Callable[..., dict[str, Any]],
        build_service_manifest: Callable[..., dict[str, Any]],
        kaniko_image: str,
        model_service_prefix: str,
        build_timeout_sec: int,
        rollout_timeout_sec: int,
        http_client_factory: Callable[[str | bool], httpx.AsyncClient] | None = None,
        registry_secret: str = "bentloy-registry-creds",
        insecure_registry_tls_default: bool = False,
    ):
        self.operation_store = operation_store
        self.k8s_client = k8s_client
        self.resolve_k8s_auth = resolve_k8s_auth
        self.build_job_manifest = build_job_manifest
        self.build_delete_job_manifest = build_delete_job_manifest
        self.build_deployment_manifest = build_deployment_manifest
        self.build_service_manifest = build_service_manifest
        self.kaniko_image = kaniko_image
        self.model_service_prefix = model_service_prefix
        self.build_timeout_sec = build_timeout_sec
        self.rollout_timeout_sec = rollout_timeout_sec
        self.http_client_factory = http_client_factory or _default_http_client_factory
        self.registry_secret = registry_secret
        self.insecure_registry_tls_default = insecure_registry_tls_default

    async def find_model_resource(
        self, namespace: str, resource_name: str
    ) -> dict[str, Any] | None:
        """Return the metadata of the Service (else Deployment) named `resource_name`.

        None when neither exists. Raises when the Kubernetes API cannot be read.
        """

        base_url, headers, verify = self.resolve_k8s_auth()
        urls = (
            f"{base_url}/api/v1/namespaces/{namespace}/services/{resource_name}",
            f"{base_url}/apis/apps/v1/namespaces/{namespace}/deployments/{resource_name}",
        )
        async with self.http_client_factory(verify) as client:
            for url in urls:
                resource = await self.k8s_client.get_resource(
                    client=client, headers=headers, resource_url=url
                )
                if resource is not None:
                    return resource.get("metadata") or {}
        return None

    async def run_operation(self, operation_id: str) -> None:
        """Execute one queued deployment operation end-to-end."""

        operation = await self.operation_store.get(operation_id)
        if operation is None:
            return

        try:
            payload: DeployOperationRequest = operation["request"]
            namespace = payload["namespace"]
            model_name = payload["model_name"]
            deployment_slug = payload["deployment_slug"]
            model_version = payload["model_version"]
            model_uri = payload["model_uri"]
            run_id = payload.get("run_id", "")
            experiment_name = payload.get("experiment_name", "")
            tracking_uri = payload["tracking_uri"]
            replicas = payload["replicas"]
            target_image = payload["target_image"]
            base_image = payload["base_image"]
            registry_secret = payload["registry_secret"]
            insecure_registry_tls = payload["insecure_registry_tls"]
            registry_server = payload["registry_server"]
            job_name = payload["job_name"]

            base_url, headers, verify = self.resolve_k8s_auth()
            await self.operation_store.update(
                operation_id,
                status="building",
                step="create_job",
            )

            job_manifest = self.build_job_manifest(
                job_name=job_name,
                namespace=namespace,
                model_name=model_name,
                model_version=model_version,
                model_uri=model_uri,
                mlflow_tracking_uri=tracking_uri,
                target_image=target_image,
                base_image=base_image,
                registry_secret=registry_secret,
                kaniko_image=self.kaniko_image,
                insecure_registry_tls=insecure_registry_tls,
                registry_server=registry_server,
            )

            jobs_url = f"{base_url}/apis/batch/v1/namespaces/{namespace}/jobs"
            job_url = f"{jobs_url}/{job_name}"

            async with self.http_client_factory(verify) as client:
                create_response = await client.post(jobs_url, headers=headers, json=job_manifest)
                if create_response.status_code not in {200, 201, 202}:
                    raise DeployStepError(
                        truncate(
                            f"Job creation failed ({create_response.status_code}): "
                            f"{extract_failure_reason(create_response.text)}"
                        ),
                        log=create_response.text,
                    )

                start = asyncio.get_running_loop().time()
                pushed_image = False
                while asyncio.get_running_loop().time() - start < self.build_timeout_sec:
                    response = await client.get(job_url, headers=headers)
                    if response.status_code != 200:
                        await asyncio.sleep(4)
                        continue

                    status = response.json().get("status", {})
                    failed = int(status.get("failed", 0))
                    succeeded = int(status.get("succeeded", 0))

                    if failed > 0:
                        prepare_logs = await self.k8s_client.read_job_logs(
                            client=client,
                            base_url=base_url,
                            headers=headers,
                            namespace=namespace,
                            job_name=job_name,
                            container="prepare-build-context",
                        )
                        kaniko_logs = await self.k8s_client.read_job_logs(
                            client=client,
                            base_url=base_url,
                            headers=headers,
                            namespace=namespace,
                            job_name=job_name,
                            container="kaniko",
                        )
                        raise build_failure(
                            prepare_logs=prepare_logs,
                            kaniko_logs=kaniko_logs,
                            target_image=target_image,
                            registry_secret=registry_secret,
                        )

                    if succeeded > 0:
                        break

                    kaniko_logs = await self.k8s_client.read_job_logs(
                        client=client,
                        base_url=base_url,
                        headers=headers,
                        namespace=namespace,
                        job_name=job_name,
                        container="kaniko",
                    )
                    if "Pushing image to" in kaniko_logs and not pushed_image:
                        pushed_image = True
                        await self.operation_store.update(
                            operation_id,
                            status="pushing",
                            step="kaniko_push",
                        )
                    await asyncio.sleep(4)
                else:
                    raise RuntimeError("Build Job timeout.")

                await self.operation_store.update(
                    operation_id,
                    status="deploying",
                    step="apply_resources",
                )

                deployment_manifest = self.build_deployment_manifest(
                    namespace=namespace,
                    model_name=model_name,
                    model_slug=deployment_slug,
                    model_image=target_image,
                    replicas=replicas,
                    tracking_uri=tracking_uri,
                    model_uri="/models/model",
                    model_version="image",
                    registry_secret=registry_secret,
                    model_service_prefix=self.model_service_prefix,
                    run_id=run_id,
                    source_model_uri=model_uri,
                    experiment_name=experiment_name,
                    operation_id=operation_id,
                )
                service_manifest = self.build_service_manifest(
                    namespace=namespace,
                    model_slug=deployment_slug,
                    model_service_prefix=self.model_service_prefix,
                    run_id=run_id,
                    source_model_uri=model_uri,
                    experiment_name=experiment_name,
                    operation_id=operation_id,
                )

                deploy_name = f"{self.model_service_prefix}-{deployment_slug}"
                service_name = f"{self.model_service_prefix}-{deployment_slug}"
                deployments_url = f"{base_url}/apis/apps/v1/namespaces/{namespace}/deployments"
                deployment_url = f"{deployments_url}/{deploy_name}"
                services_url = f"{base_url}/api/v1/namespaces/{namespace}/services"
                service_url = f"{services_url}/{service_name}"

                await self.k8s_client.ensure_resource(
                    client=client,
                    headers=headers,
                    list_url=deployments_url,
                    resource_url=deployment_url,
                    manifest=deployment_manifest,
                )
                await self.k8s_client.ensure_resource(
                    client=client,
                    headers=headers,
                    list_url=services_url,
                    resource_url=service_url,
                    manifest=service_manifest,
                )

                await self.operation_store.update(operation_id, status="deploying", step="rollout")
                await self.k8s_client.wait_deployment_ready(
                    client=client,
                    headers=headers,
                    deployment_url=deployment_url,
                    replicas=replicas,
                    timeout_sec=self.rollout_timeout_sec,
                )

            await self.operation_store.update(
                operation_id,
                status="ready",
                step="completed",
                message="Deployment completed successfully.",
            )

        except DeployStepError as exc:
            await self.operation_store.update(
                operation_id,
                status="failed",
                step="failed",
                error=exc.message,
                error_log=exc.log,
            )
        except Exception as exc:
            message = str(exc) or type(exc).__name__
            await self.operation_store.update(
                operation_id,
                status="failed",
                step="failed",
                error=truncate(message),
                error_log=message if len(message) > MAX_ERROR_CHARS else None,
            )

    def _model_urls(self, base_url: str, namespace: str, resource_name: str) -> tuple[str, str]:
        """Return the (Deployment, Service) URLs of a model-service resource."""

        return (
            f"{base_url}/apis/apps/v1/namespaces/{namespace}/deployments/{resource_name}",
            f"{base_url}/api/v1/namespaces/{namespace}/services/{resource_name}",
        )

    @staticmethod
    def _deployment_images(deployment: dict[str, Any]) -> set[str]:
        pod_spec = deployment.get("spec", {}).get("template", {}).get("spec", {})
        containers = pod_spec.get("containers", []) + pod_spec.get("initContainers", [])
        return {container.get("image", "") for container in containers}

    async def _image_user(
        self,
        client: httpx.AsyncClient,
        base_url: str,
        headers: dict[str, str],
        namespace: str,
        image_ref: str,
        exclude_deployment: str = "",
        exclude_operation_id: str = "",
    ) -> str | None:
        """Describe who else uses `image_ref`, or None if nobody does.

        The image name is `model-<model_name slug>:v<version>-r<run>`, independent of the
        deployed name, so several deployments (and a deploy still in progress) can share it.
        Deleting it would break them on their next pod start. Checks the Deployments of
        `namespace` (one LIST call) and the in-memory operations.
        """

        deployments_url = f"{base_url}/apis/apps/v1/namespaces/{namespace}/deployments"
        for deployment in await self.k8s_client.list_resources(client, headers, deployments_url):
            metadata = deployment.get("metadata", {})
            name = metadata.get("name", "")
            if name == exclude_deployment or metadata.get("deletionTimestamp"):
                continue
            if image_ref in self._deployment_images(deployment):
                return f"Deployment {name}"

        operation = await self.operation_store.find_deploying_image(
            image_ref, exclude_operation_id=exclude_operation_id
        )
        if operation is not None:
            return f"operation {operation['operation_id']} ({operation['status']})"
        return None

    async def _delete_image_unless_used(
        self,
        client: httpx.AsyncClient,
        base_url: str,
        headers: dict[str, str],
        *,
        namespace: str,
        image_ref: str,
        deployment_slug: str,
        registry_secret: str,
        insecure_registry_tls: bool,
        exclude_deployment: str = "",
        exclude_operation_id: str = "",
    ) -> str:
        """Delete `image_ref` from the registry unless something else uses it; return a note."""

        user = await self._image_user(
            client,
            base_url,
            headers,
            namespace,
            image_ref,
            exclude_deployment=exclude_deployment,
            exclude_operation_id=exclude_operation_id,
        )
        if user:
            return f"Image {image_ref} kept: still used by {user}."
        await self._delete_registry_image(
            image_ref=image_ref,
            namespace=namespace,
            deployment_slug=deployment_slug,
            registry_secret=registry_secret,
            insecure_registry_tls=insecure_registry_tls,
        )
        return f"Image {image_ref} deleted."

    @staticmethod
    def _owner_operation(resource: dict[str, Any] | None) -> str | None:
        if resource is None:
            return None
        annotations = resource.get("metadata", {}).get("annotations") or {}
        return annotations.get(OPERATION_ID_ANNOTATION) or None

    async def delete_operation_resources(
        self,
        operation: DeployOperationRecord,
        registry_secret: str,
        insecure_registry_tls: bool,
    ) -> list[str]:
        """Delete what one deploy operation created; return notes on what was done.

        - Deployment and Service `<prefix>-<deployment_slug>`: only if their
          `bentloy.io/operation-id` annotation is this operation. Otherwise they belong to
          another deploy (e.g. a later overwrite, or resources from before the annotation
          existed) and are kept.
        - The build Job.
        - The image, unless another Deployment or a deploy in progress uses it.
        """

        operation_id = operation["operation_id"]
        request = operation.get("request") or {}
        namespace = request.get("namespace") or operation.get("namespace") or ""
        deployment_slug = request.get("deployment_slug") or to_model_slug(
            operation.get("deployed_name") or ""
        )
        resource_name = f"{self.model_service_prefix}-{deployment_slug}" if deployment_slug else ""
        notes: list[str] = []
        deleted_deployment = ""

        base_url, headers, verify = self.resolve_k8s_auth()
        async with self.http_client_factory(verify) as client:
            if resource_name:
                deployment_url, service_url = self._model_urls(base_url, namespace, resource_name)
                for kind, url in (("Deployment", deployment_url), ("Service", service_url)):
                    resource = await self.k8s_client.get_resource(client, headers, url)
                    if resource is None:
                        continue
                    owner = self._owner_operation(resource)
                    if owner != operation_id:
                        notes.append(
                            f"{kind} {resource_name} kept: owned by "
                            f"{owner or 'a deploy without operation id'}."
                        )
                        continue
                    await self.k8s_client.delete_resource(client, headers, url)
                    notes.append(f"{kind} {resource_name} deleted.")
                    if kind == "Deployment":
                        deleted_deployment = resource_name

            job_name = operation.get("k8s_job")
            if job_name:
                job_url = f"{base_url}/apis/batch/v1/namespaces/{namespace}/jobs/{job_name}"
                if await self.k8s_client.delete_resource(client, headers, job_url):
                    notes.append(f"Job {job_name} deleted.")

            image_ref = operation.get("image_ref")
            if image_ref and deployment_slug:
                notes.append(
                    await self._delete_image_unless_used(
                        client,
                        base_url,
                        headers,
                        namespace=namespace,
                        image_ref=image_ref,
                        deployment_slug=deployment_slug,
                        registry_secret=registry_secret,
                        insecure_registry_tls=insecure_registry_tls,
                        exclude_deployment=deleted_deployment,
                        exclude_operation_id=operation_id,
                    )
                )
        return notes

    async def _delete_registry_image(
        self, 
        image_ref: str,
        namespace: str,
        deployment_slug: str,
        registry_secret: str,
        insecure_registry_tls: bool = False
    ) -> None:
        """Executes a K8s Job to delete the image from the registry using Crane."""
        
        # Generate a safe, short job name
        job_hash = str(hash(image_ref))[-6:]
        # K8s limits names to 63 chars
        job_name = f"bentloy-delete-img-{deployment_slug}"[:56].rstrip("-") + f"-{job_hash}"

        job_manifest = self.build_delete_job_manifest(
            job_name=job_name,
            namespace=namespace,
            model_slug=deployment_slug,
            image_ref=image_ref,
            registry_secret=registry_secret,
            insecure_registry_tls=insecure_registry_tls
        )

        base_url, headers, verify = self.resolve_k8s_auth()
        jobs_url = f"{base_url}/apis/batch/v1/namespaces/{namespace}/jobs"
        job_url = f"{jobs_url}/{job_name}"

        async with self.http_client_factory(verify) as client:
            # 1. Create Job
            create_response = await client.post(jobs_url, headers=headers, json=job_manifest)
            if create_response.status_code not in {200, 201, 202}:
                raise RuntimeError(
                    f"Delete Job creation failed ({create_response.status_code}): {create_response.text}"
                )

            # 2. Wait for Completion
            start = asyncio.get_running_loop().time()
            timeout_sec = 120 # Crane deletions are usually instant, 2 mins is plenty
            
            while asyncio.get_running_loop().time() - start < timeout_sec:
                response = await client.get(job_url, headers=headers)
                if response.status_code != 200:
                    await asyncio.sleep(4)
                    continue

                status = response.json().get("status", {})
                failed = int(status.get("failed", 0))
                succeeded = int(status.get("succeeded", 0))

                if failed > 0:
                    crane_logs = await self.k8s_client.read_job_logs(
                        client=client,
                        base_url=base_url,
                        headers=headers,
                        namespace=namespace,
                        job_name=job_name,
                        container="crane",
                    )
                    
                    # Gracefully handle if the image is already missing from the registry
                    if "MANIFEST_UNKNOWN" in crane_logs or "not found" in crane_logs.lower():
                        return
                    
                    raise RuntimeError(f"Image Deletion Job failed.\n[crane]\n{crane_logs}")

                if succeeded > 0:
                    break

                await asyncio.sleep(4)
            else:
                raise RuntimeError("Image Deletion Job timeout.")

    async def delete_deployed_model(self, service_name: str, namespace: str) -> dict[str, Any]:
        """Delete a deployed model: its Deployment, Service and (if unused) image.

        Only `<prefix>-*` names are accepted, so this endpoint cannot remove other
        workloads of the namespace (e.g. the gateway itself).
        """

        prefix = f"{self.model_service_prefix}-"
        if not service_name.startswith(prefix) or service_name == prefix:
            return {
                "status": "error",
                "message": f"'{service_name}' is not a model service (expected '{prefix}<name>').",
            }
        deployment_slug = service_name[len(prefix):]

        base_url, headers, verify = self.resolve_k8s_auth()
        async with self.http_client_factory(verify) as client:
            deployment_url, service_url = self._model_urls(base_url, namespace, service_name)
            deployment = await self.k8s_client.get_resource(client, headers, deployment_url)
            images = sorted(self._deployment_images(deployment)) if deployment else []

            notes = []
            for kind, url in (("Deployment", deployment_url), ("Service", service_url)):
                if await self.k8s_client.delete_resource(client, headers, url):
                    notes.append(f"{kind} {service_name} deleted.")

            for image_ref in images:
                try:
                    notes.append(
                        await self._delete_image_unless_used(
                            client,
                            base_url,
                            headers,
                            namespace=namespace,
                            image_ref=image_ref,
                            deployment_slug=deployment_slug,
                            registry_secret=self.registry_secret,
                            insecure_registry_tls=self.insecure_registry_tls_default,
                            exclude_deployment=service_name,
                        )
                    )
                except Exception as img_err:
                    logger.warning("Failed to delete registry image %s: %s", image_ref, img_err)
                    return {
                        "status": "partial_success",
                        "message": " ".join(notes)
                        + f" Registry image cleanup failed: {img_err}",
                    }

        if not notes:
            return {"status": "error", "message": f"Model {service_name} not found."}
        return {"status": "success", "message": " ".join(notes)}
