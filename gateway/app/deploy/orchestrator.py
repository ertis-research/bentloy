import asyncio
from typing import Any, Callable

import httpx

from ..domain.types import DeployOperationRequest
from ..kubernetes.client import KubernetesApiClient
from .operation_store import DeployOperationStore


def _default_http_client_factory(verify: str | bool) -> httpx.AsyncClient:
    return httpx.AsyncClient(timeout=20.0, verify=verify)


class DeployOrchestrator:
    """Executes asynchronous model deploy operations against Kubernetes."""

    def __init__(
        self,
        operation_store: DeployOperationStore,
        k8s_client: KubernetesApiClient,
        resolve_k8s_auth: Callable[[], tuple[str, dict[str, str], str | bool]],
        build_job_manifest: Callable[..., dict[str, Any]],
        build_deployment_manifest: Callable[..., dict[str, Any]],
        build_service_manifest: Callable[..., dict[str, Any]],
        kaniko_image: str,
        model_service_prefix: str,
        build_timeout_sec: int,
        rollout_timeout_sec: int,
        http_client_factory: Callable[[str | bool], httpx.AsyncClient] | None = None,
    ):
        self.operation_store = operation_store
        self.k8s_client = k8s_client
        self.resolve_k8s_auth = resolve_k8s_auth
        self.build_job_manifest = build_job_manifest
        self.build_deployment_manifest = build_deployment_manifest
        self.build_service_manifest = build_service_manifest
        self.kaniko_image = kaniko_image
        self.model_service_prefix = model_service_prefix
        self.build_timeout_sec = build_timeout_sec
        self.rollout_timeout_sec = rollout_timeout_sec
        self.http_client_factory = http_client_factory or _default_http_client_factory

    async def run_operation(self, operation_id: str) -> None:
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
                    raise RuntimeError(
                        f"Job creation failed ({create_response.status_code}): {create_response.text}"
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
                        raise RuntimeError(
                            "Build Job failed.\n\n"
                            f"[prepare-build-context]\n{prepare_logs}\n\n"
                            f"[kaniko]\n{kaniko_logs}"
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
                )
                service_manifest = self.build_service_manifest(
                    namespace,
                    deployment_slug,
                    self.model_service_prefix,
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

        except Exception as exc:
            await self.operation_store.update(
                operation_id,
                status="failed",
                step="failed",
                error=str(exc),
            )
