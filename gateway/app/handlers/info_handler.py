"""Gateway information endpoints for health, discovery, and deploy summaries."""

import math
from typing import Any, Callable

import httpx

from ..deploy.operation_store import DeployOperationStore
from ..routing.discovery import KubernetesModelDiscovery, sort_newest_first


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

    async def discover_k8s_model_services(self) -> tuple[list[dict[str, str | None]], str | None]:
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

    async def list_models(
        self,
        page: int,
        page_size: int,
        query: str,
        experiment_name: str = "",
        run_id: str = "",
    ) -> dict[str, Any]:
        """List available models from env config, recent traffic, and Kubernetes discovery.

        `query` is a case-insensitive substring of the deployment name. `experiment_name`
        and `run_id` are exact filters on discovered models and deploy operations; when
        either is set, configured and recently routed models (which belong to no
        experiment) are left out.
        """
        query_lower = (query or "").lower()
        experiment_name = (experiment_name or "").strip()
        run_id = (run_id or "").strip()
        model_filter = bool(experiment_name or run_id)

        # Safely clamp the page to a minimum of 0 (0-indexed pagination)
        safe_page = max(0, page)

        # 1. Gather and filter base lists
        configured_names = [] if model_filter else sorted([
            name for name in self.model_urls.keys() if query_lower in name.lower()
        ])

        recently_routed_names = [] if model_filter else sorted([
            name for name in self.recently_routed_models.keys() if query_lower in name.lower()
        ])

        def matches_model(item: dict[str, Any]) -> bool:
            return (
                (not experiment_name or item.get("experiment_name") == experiment_name)
                and (not run_id or item.get("run_id") == run_id)
            )

        discovered_services, discovery_error = await self.discover_k8s_model_services()
        # Newest deployments first.
        discovered_filtered = sort_newest_first([
            svc for svc in discovered_services
            if query_lower in svc["inferred_model_name"].lower() and matches_model(svc)
        ])

        # 2. Build and filter the combined endpoints dictionary
        all_endpoints = {}
        all_endpoints.update({name: f"{self.model_urls[name]}/predict" for name in configured_names})
        all_endpoints.update({name: f"{self.recently_routed_models[name]}/predict" for name in recently_routed_names})
        all_endpoints.update({svc["inferred_model_name"]: svc["predict_endpoint"] for svc in discovered_filtered})
        # Discovered models keep their newest-first order; models without a creation
        # date (configured or only recently routed) follow, alphabetically.
        discovered_keys = list(dict.fromkeys(svc["inferred_model_name"] for svc in discovered_filtered))
        endpoints_keys = discovered_keys + sorted(set(all_endpoints) - set(discovered_keys))

        # 3. Gather operations
        all_operations = await self.operation_store.list_summaries(
            limit=1000,
            experiment_name=experiment_name,
            run_id=run_id,
        )
        # `query` matches the deployed name, as it does for discovered models.
        if query_lower:
            all_operations = [
                op for op in all_operations
                if query_lower in (op.get("deployed_name") or "").lower()
            ]

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
                # `page` is shared by every list, so an out-of-range page still returns
                # an empty slice (clamping would repeat items while the client walks the
                # longest list). An empty list never has a previous page.
                "has_previous": safe_page > 0 and total_pages > 0,
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
