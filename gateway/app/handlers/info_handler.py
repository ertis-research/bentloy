"""Gateway information endpoints for health, discovery, and deploy summaries."""

import math
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

    async def list_models(self, page: int, page_size: int, query: str) -> dict[str, Any]:
        """List available models from env config, recent traffic, and Kubernetes discovery."""
        query_lower = (query or "").lower()
        
        # Safely clamp the page to a minimum of 0 (0-indexed pagination)
        safe_page = max(0, page)

        # 1. Gather and filter base lists
        configured_names = sorted([
            name for name in self.model_urls.keys() if query_lower in name.lower()
        ])
        
        recently_routed_names = sorted([
            name for name in self.recently_routed_models.keys() if query_lower in name.lower()
        ])

        discovered_services, discovery_error = await self.discover_k8s_model_services()
        discovered_filtered = sorted(
            [svc for svc in discovered_services if query_lower in svc["inferred_model_name"].lower()],
            key=lambda x: x["inferred_model_name"]
        )

        # 2. Build and filter the combined endpoints dictionary
        all_endpoints = {}
        all_endpoints.update({name: f"{self.model_urls[name]}/predict" for name in configured_names})
        all_endpoints.update({name: f"{self.recently_routed_models[name]}/predict" for name in recently_routed_names})
        all_endpoints.update({svc["inferred_model_name"]: svc["predict_endpoint"] for svc in discovered_filtered})
        endpoints_keys = sorted(all_endpoints.keys())

        # 3. Gather operations
        all_operations = await self.operation_store.list_summaries(limit=1000)

        # 4. Helper function to calculate slices and metadata per list (0-indexed)
        def paginate_list(data_length: int):
            total_pages = math.ceil(data_length / page_size) if page_size > 0 else 0
            
            # 0-indexed start and end calculation
            start = safe_page * page_size
            end = start + page_size
            
            return start, end, {
                "total_items": data_length,
                "total_pages": total_pages,
                "current_page": safe_page,
                "page_size": page_size,
                # In 0-indexed, the last page is total_pages - 1
                "has_next": safe_page < (total_pages - 1) if total_pages > 0 else False,
                "has_previous": safe_page > 0
            }

        # 5. Calculate indices and metadata for each list
        c_start, c_end, c_meta = paginate_list(len(configured_names))
        r_start, r_end, r_meta = paginate_list(len(recently_routed_names))
        d_start, d_end, d_meta = paginate_list(len(discovered_filtered))
        e_start, e_end, e_meta = paginate_list(len(endpoints_keys))
        o_start, o_end, o_meta = paginate_list(len(all_operations))

        # 6. Apply the slices to the data
        paginated_endpoints = {k: all_endpoints[k] for k in endpoints_keys[e_start:e_end]}

        return {
            "configured_models": configured_names[c_start:c_end],
            "recently_routed_models": recently_routed_names[r_start:r_end],
            "discovered_models": discovered_filtered[d_start:d_end],
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
            "deploy_operations": all_operations[o_start:o_end],
            "endpoints": paginated_endpoints,
            "pagination": {
                "configured_models": c_meta,
                "recently_routed_models": r_meta,
                "discovered_models": d_meta,
                "endpoints": e_meta,
                "deploy_operations": o_meta
            }
        }
