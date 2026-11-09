"""Asynchronous deployment orchestration against the Kubernetes API."""

import asyncio
from typing import Any, Callable
import re
from kubernetes_asyncio import client, config
from kubernetes_asyncio.client.exceptions import ApiException
from kubernetes_asyncio.config.config_exception import ConfigException

import httpx

from ..domain.types import DeployOperationRequest
from ..kubernetes.client import KubernetesApiClient
from .operation_store import DeployOperationStore


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

    async def _delete_k8s_resources(self, deployed_name: str, job_name:str | None, namespace: str) -> None:
        """Helper to delete Job, Deployment, and Pods safely."""
        
        # # 1. Load the Kubernetes configuration
        # try:
        #     # Try loading the in-cluster config (when running inside a Kubernetes Pod)
        #     config.load_incluster_config()
        # except ConfigException:
        #     # Fallback to local kubeconfig (when testing locally on your laptop)
        #     await config.load_kube_config()

        # # 2. Initialize the client and APIs
        # async with client.ApiClient() as api_client:
        #     apps_v1 = client.AppsV1Api(api_client)
        #     batch_v1 = client.BatchV1Api(api_client)

        #     # 3. Delete Deployment
        #     try:
        #         await apps_v1.delete_namespaced_deployment(
        #             name=deployed_name, 
        #             namespace=namespace
        #         )
        #         await apps_v1
        #     except ApiException as e:
        #         # 404 means it's already deleted/gone, which is fine
        #         if e.status != 404: 
        #             raise

        #     # 4. Delete Job
        #     try:
        #         if job_name: 
        #             await batch_v1.delete_namespaced_job(
        #                 name=job_name, 
        #                 namespace=namespace,
        #                 propagation_policy="Background" # Ensures spawned pods are also deleted
        #             )
        #     except ApiException as e:
        #         if e.status != 404: 
        #             raise
        #     # (Optional) Delete specific Pods if they aren't bound to the Job/Deployment
        #     # Usually K8s garbage collection handles pods tied to Jobs/Deployments automatically.

        try:
            config.load_incluster_config()
        except ConfigException:
            await config.load_kube_config()

        # 2. Initialize the client and APIs
        async with client.ApiClient() as api_client:
            apps_v1 = client.AppsV1Api(api_client)
            batch_v1 = client.BatchV1Api(api_client)
            core_v1 = client.CoreV1Api(api_client)

            # 3. Delete Deployment
            try:
                await apps_v1.delete_namespaced_deployment(
                    name=deployed_name, 
                    namespace=namespace
                )
            except ApiException as e:
                if e.status != 404: raise

            # 4. Delete Job
            try:
                if job_name: 
                    await batch_v1.delete_namespaced_job(
                        name=job_name, 
                        namespace=namespace,
                        propagation_policy="Background"
                    )
            except ApiException as e:
                if e.status != 404: raise
            
            # 5. Delete Orphaned Pods Safely via Regex
            try:
                pod_list = await core_v1.list_namespaced_pod(namespace=namespace)
                
                # Regex pattern explained:
                # ^{deployed_name}        -> Must start EXACTLY with deployed_name
                # (?:-[a-f0-9]{7,12})?    -> OPTIONAL ReplicaSet hex hash (Deployments have this, Jobs don't)
                # -[a-z0-9]{5}$           -> EXACTLY one 5-character Kubernetes pod hash at the end
                safe_deploy_pattern = re.compile(rf"^{re.escape(deployed_name)}(?:-[a-f0-9]{{7,12}})?-[a-z0-9]{{5}}$")
                
                if job_name:
                    safe_job_pattern = re.compile(rf"^{re.escape(job_name)}-[a-z0-9]{{5}}$")
                else:
                    safe_job_pattern = None

                for pod in pod_list.items:
                    pod_name = pod.metadata.name
                    
                    # Check if the pod name matches the safe regex patterns
                    is_match = False
                    if safe_deploy_pattern.match(pod_name):
                        is_match = True
                    elif safe_job_pattern and safe_job_pattern.match(pod_name):
                        is_match = True

                    # Also gracefully handle exact matches if a bare pod was created
                    elif pod_name in (deployed_name, job_name):
                        is_match = True

                    if is_match:
                        try:
                            await core_v1.delete_namespaced_pod(
                                name=pod_name,
                                namespace=namespace
                            )
                            print(f"Cleaned up orphaned pod: {pod_name}")
                        except ApiException as pod_e:
                            if pod_e.status != 404:
                                raise

            except ApiException as list_e:
                raise

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
        job_name = f"delete-img-{deployment_slug}-{job_hash}"[:63] # K8s limits names to 63 chars

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

    async def delete_deployed_model(self, service_name, namespace):
        # namespace = getattr(self, "model_namespace", "default")
                
        # Derive the deployment slug for the crane job name
        prefix = f"{getattr(self, 'model_service_prefix', 'model')}-"
        deployment_slug = service_name.replace(prefix, "") if service_name.startswith(prefix) else service_name

        image_ref = None
        registry_secret = "default-registry-secret" # Fallback if none is found
        
        try:
            config.load_incluster_config()
        except ConfigException:
            await config.load_kube_config()

        async with client.ApiClient() as api_client:
            apps_v1 = client.AppsV1Api(api_client)
            core_v1 = client.CoreV1Api(api_client)

            # 2. Discover Metadata BEFORE deleting the K8s resources
            try:
                # Read the deployment to get image_ref and secret
                deployment = await apps_v1.read_namespaced_deployment(name=service_name, namespace=namespace)
                
                # Extract the image reference
                if deployment.spec.template.spec.containers:
                    image_ref = deployment.spec.template.spec.containers[0].image
                
                # Extract the registry secret
                if deployment.spec.template.spec.image_pull_secrets:
                    registry_secret = deployment.spec.template.spec.image_pull_secrets[0].name

            except ApiException as e:
                if e.status == 404:
                    print(f"Deployment {service_name} not found. Proceeding with cleanup anyway.")
                else:
                    raise Exception(f"Failed to read Deployment from K8s API: {str(e)}")

            # 3. Call the robust cleanup helper we just built!
            # Since we only have `service_name` (and no operation record), we don't definitively 
            # know the Kaniko build job name. Passing None is safe, as K8s Jobs usually have TTLs, 
            # and our safe regex cleanup will catch the Deployment pods perfectly.
            # If your jobs follow a strict naming convention (e.g., f"build-{service_name}"), you can pass it.
            job_name = None  
            
            await self._delete_k8s_resources(
                deployed_name=service_name, 
                job_name=job_name, 
                namespace=namespace
            )

            # 4. Delete the associated K8s Service (which routes traffic to the model)
            try:
                await core_v1.delete_namespaced_service(name=service_name, namespace=namespace)
                print(f"Deleted K8s Service: {service_name}")
            except ApiException as e:
                if e.status != 404: 
                    raise

        # 5. Delete the Image from the Registry (using the Crane helper)
        if image_ref:
            print(f"Triggering registry deletion for: {image_ref}")
            try:
                await self._delete_registry_image(
                    image_ref=image_ref,
                    namespace=namespace,
                    deployment_slug=deployment_slug,
                    registry_secret=registry_secret,
                    insecure_registry_tls=getattr(self, "insecure_registry_tls", False) 
                )
            except Exception as img_err:
                print(f"Failed to delete registry image: {img_err}")
                return {
                    "status": "partial_success", 
                    "message": f"K8s resources deleted, but registry image cleanup failed: {img_err}"
                }
        else:
            print("No image_ref found. Skipping registry deletion.")

        return {
            "status": "success", 
            "message": f"Model {service_name} and its K8s/Registry resources were successfully deleted."
        }
