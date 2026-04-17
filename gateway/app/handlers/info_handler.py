"""Gateway information endpoints for health, discovery, and deploy summaries."""

from typing import Any, Callable

import httpx

from ..deploy.operation_store import DeployOperationStore
from ..routing.discovery import KubernetesModelDiscovery


def _default_client_factory(timeout: float) -> httpx.AsyncClient:
    """Build the default async HTTP client used by info requests."""

    return httpx.AsyncClient(timeout=timeout)


class InfoHandler:
    """Health and model listing request handling."""

    def __init__(
        self,
        model_urls: dict[str, str],
        model_service_prefix: str,
        model_namespace: str,
        model_url_template: str,
        recently_routed_models: dict[str, str],
        model_discovery: KubernetesModelDiscovery,
        operation_store: DeployOperationStore,
        resolve_k8s_auth: Callable[[], tuple[str, dict[str, str], str | bool]],
        client_factory: Callable[[float], httpx.AsyncClient] | None = None,
    ):
        self.model_urls = model_urls
        self.model_service_prefix = model_service_prefix
        self.model_namespace = model_namespace
        self.model_url_template = model_url_template
        self.recently_routed_models = recently_routed_models
        self.model_discovery = model_discovery
        self.operation_store = operation_store
        self.resolve_k8s_auth = resolve_k8s_auth
        self.client_factory = client_factory or _default_client_factory

    async def discover_k8s_model_services(self) -> tuple[list[dict[str, str]], str | None]:
        """
        Discover model-<slug> services using Kubernetes API from inside the pod.
        If discovery is not possible (RBAC, outside cluster, etc.), return a descriptive error.
        """
        try:
            base_url, headers, verify = self.resolve_k8s_auth()
        except Exception as exc:
            return [], str(exc)

        return await self.model_discovery.discover_services(
            base_url=base_url,
            headers=headers,
            verify=verify,
        )

    async def health(self) -> dict[str, Any]:
        """Health check for models explicitly configured by env."""
        health_status: dict[str, Any] = {}

        async with self.client_factory(5.0) as client:
            for model_name, base_url in self.model_urls.items():
                try:
                    response = await client.get(f"{base_url}/health")
                    health_status[model_name] = response.json()
                except Exception as exc:
                    health_status[model_name] = {
                        "status": "unhealthy",
                        "error": str(exc),
                    }

        return {
            "gateway": "healthy",
            "models": health_status,
            "dynamic_routing": {
                "enabled": True,
                "service_prefix": self.model_service_prefix,
                "url_template": self.model_url_template,
            },
        }

    async def list_models(self) -> dict[str, Any]:
        """List available models from env config, recent traffic, and Kubernetes discovery."""
        configured_endpoints = {
            name: f"{url}/predict" for name, url in self.model_urls.items()
        }

        recently_routed_endpoints = {
            name: f"{url}/predict"
            for name, url in self.recently_routed_models.items()
        }

        discovered_services, discovery_error = await self.discover_k8s_model_services()
        discovered_endpoints = {
            service["inferred_model_name"]: service["predict_endpoint"]
            for service in discovered_services
        }

        endpoints: dict[str, str] = {}
        endpoints.update(configured_endpoints)
        endpoints.update(discovered_endpoints)
        endpoints.update(recently_routed_endpoints)

        operations_summary = await self.operation_store.list_summaries(limit=20)

        return {
            "configured_models": sorted(self.model_urls.keys()),
            "recently_routed_models": sorted(self.recently_routed_models.keys()),
            "discovered_models": discovered_services,
            "dynamic_routing": {
                "enabled": True,
                "service_prefix": self.model_service_prefix,
                "url_template": self.model_url_template,
                "namespace": self.model_namespace,
                "external_url_support": {
                    "enabled": True,
                    "field": "model_url",
                    "accepted_schemes": ["http", "https"],
                    "priority": "model_url > MODELO_*_URL > model-<slug> convention",
                },
                "how_to_call": (
                    "If model_name is not in configured_models, "
                    "it is resolved using model-<slug> convention."
                ),
            },
            "discovery": {
                "source": "kubernetes_services",
                "status": "ok" if discovery_error is None else "error",
                "error": discovery_error,
            },
            "deploy_operations": operations_summary,
            "endpoints": endpoints,
        }
