"""Minimal Kubernetes API client operations used by deployment workflows."""

import asyncio
from typing import Any

import httpx


class KubernetesApiClient:
    """Provide high-level async helpers over raw Kubernetes HTTP calls."""

    async def get_job_pod_name(
        self,
        client: httpx.AsyncClient,
        base_url: str,
        headers: dict[str, str],
        namespace: str,
        job_name: str,
    ) -> str | None:
        """Return the newest pod name created for a given Job, if available."""

        pods_url = f"{base_url}/api/v1/namespaces/{namespace}/pods"
        response = await client.get(
            pods_url,
            headers=headers,
            params={"labelSelector": f"job-name={job_name}"},
        )
        if response.status_code != 200:
            return None

        items = response.json().get("items", [])
        if not items:
            return None

        items.sort(
            key=lambda item: item.get("metadata", {}).get("creationTimestamp", ""),
            reverse=True,
        )
        return items[0].get("metadata", {}).get("name")

    async def read_job_logs(
        self,
        client: httpx.AsyncClient,
        base_url: str,
        headers: dict[str, str],
        namespace: str,
        job_name: str,
        container: str,
    ) -> str:
        """Read recent logs for a Job container, returning an empty string on failure."""

        pod_name = await self.get_job_pod_name(
            client=client,
            base_url=base_url,
            headers=headers,
            namespace=namespace,
            job_name=job_name,
        )
        if not pod_name:
            return ""

        logs_url = f"{base_url}/api/v1/namespaces/{namespace}/pods/{pod_name}/log"
        response = await client.get(
            logs_url,
            headers=headers,
            params={"container": container, "tailLines": 200},
        )
        if response.status_code != 200:
            return ""
        return response.text

    async def get_resource(
        self,
        client: httpx.AsyncClient,
        headers: dict[str, str],
        resource_url: str,
    ) -> dict[str, Any] | None:
        """Return a Kubernetes resource as a dict, or None if it does not exist."""

        response = await client.get(resource_url, headers=headers)
        if response.status_code == 404:
            return None
        if response.status_code != 200:
            raise RuntimeError(f"Read failed ({response.status_code}): {response.text}")
        return response.json()

    async def list_resources(
        self,
        client: httpx.AsyncClient,
        headers: dict[str, str],
        list_url: str,
    ) -> list[dict[str, Any]]:
        """Return the items of a Kubernetes list endpoint."""

        response = await client.get(list_url, headers=headers)
        if response.status_code != 200:
            raise RuntimeError(f"List failed ({response.status_code}): {response.text}")
        return response.json().get("items", [])

    async def delete_resource(
        self,
        client: httpx.AsyncClient,
        headers: dict[str, str],
        resource_url: str,
    ) -> bool:
        """Delete a resource and its dependents (background). False if it did not exist."""

        response = await client.request(
            "DELETE",
            resource_url,
            headers=headers,
            json={"propagationPolicy": "Background"},
        )
        if response.status_code == 404:
            return False
        if response.status_code not in {200, 202}:
            raise RuntimeError(f"Delete failed ({response.status_code}): {response.text}")
        return True

    async def ensure_resource(
        self,
        client: httpx.AsyncClient,
        headers: dict[str, str],
        list_url: str,
        resource_url: str,
        manifest: dict[str, Any],
    ) -> None:
        """Create or merge-patch a Kubernetes resource from a manifest."""

        get_response = await client.get(resource_url, headers=headers)
        if get_response.status_code == 404:
            create_response = await client.post(list_url, headers=headers, json=manifest)
            if create_response.status_code not in {200, 201, 202}:
                raise RuntimeError(
                    f"Create failed ({create_response.status_code}): {create_response.text}"
                )
            return

        if get_response.status_code != 200:
            raise RuntimeError(
                f"Read failed ({get_response.status_code}): {get_response.text}"
            )

        patch_response = await client.patch(
            resource_url,
            headers={**headers, "Content-Type": "application/merge-patch+json"},
            json=manifest,
        )
        if patch_response.status_code not in {200, 201, 202}:
            raise RuntimeError(
                f"Patch failed ({patch_response.status_code}): {patch_response.text}"
            )

    async def wait_deployment_ready(
        self,
        client: httpx.AsyncClient,
        headers: dict[str, str],
        deployment_url: str,
        replicas: int,
        timeout_sec: int,
    ) -> None:
        """Poll a Deployment until all requested replicas are updated and ready."""

        start = asyncio.get_running_loop().time()
        while asyncio.get_running_loop().time() - start < timeout_sec:
            response = await client.get(deployment_url, headers=headers)
            if response.status_code != 200:
                await asyncio.sleep(3)
                continue

            payload = response.json()
            status = payload.get("status", {})
            metadata = payload.get("metadata", {})
            observed_generation = status.get("observedGeneration", 0)
            generation = metadata.get("generation", 0)
            ready = int(status.get("readyReplicas", 0))
            available = int(status.get("availableReplicas", 0))
            updated = int(status.get("updatedReplicas", 0))

            if (
                observed_generation >= generation
                and ready >= replicas
                and available >= replicas
                and updated >= replicas
            ):
                return

            await asyncio.sleep(3)

        raise RuntimeError("Deployment rollout timeout.")
