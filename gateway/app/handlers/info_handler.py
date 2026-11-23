"""Gateway information endpoints for health, discovery, and deploy summaries."""

import math
from typing import Any, Callable

import httpx

from ..deploy.operation_store import DeployOperationStore
from ..routing.discovery import KubernetesModelDiscovery, sort_newest_first


class ListModelsRequestError(Exception):
    """A list_models request with invalid parameters; carries the HTTP status to answer with."""

    def __init__(self, message: str, status_code: int = 400):
        super().__init__(message)
        self.message = message
        self.status_code = status_code


def paginate(items: list[Any], page: int, page_size: int) -> tuple[list[Any], dict[str, Any]]:
    """Return the 1-indexed `page` of `items` and its pagination metadata.

    A page past the end returns an empty slice (it is not clamped to the last page):
    `page` is shared by every list in /list_models, and clamping would repeat items
    while a client walks the longest list.
    """

    total_items = len(items)
    total_pages = math.ceil(total_items / page_size) if page_size > 0 else 0
    start = (page - 1) * page_size
    return items[start:start + page_size], {
        "total_items": total_items,
        "total_pages": total_pages,
        "current_page": page,
        "page_size": page_size,
        "has_next": page < total_pages,
        "has_previous": page > 1,
    }


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

        `page` is 1-indexed and shared by every list; `page < 1` raises
        ListModelsRequestError (400).
        """
        if page < 1:
            raise ListModelsRequestError(f"page must be >= 1 (got {page}); pagination starts at 1.")

        query_lower = (query or "").lower()
        experiment_name = (experiment_name or "").strip()
        run_id = (run_id or "").strip()
        model_filter = bool(experiment_name or run_id)

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

        # 4. Paginate each list with the same page
        configured_page, c_meta = paginate(configured_names, page, page_size)
        routed_page, r_meta = paginate(recently_routed_names, page, page_size)
        discovered_page, d_meta = paginate(discovered_filtered, page, page_size)
        endpoint_keys_page, e_meta = paginate(endpoints_keys, page, page_size)
        operations_page, o_meta = paginate(all_operations, page, page_size)
        paginated_endpoints = {k: all_endpoints[k] for k in endpoint_keys_page}

        return {
            "configured_models": configured_page,
            "recently_routed_models": routed_page,
            "discovered_models": discovered_page,
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
            "deploy_operations": operations_page,
            "endpoints": paginated_endpoints,
            "pagination": {
                "configured_models": c_meta,
                "recently_routed_models": r_meta,
                "discovered_models": d_meta,
                "endpoints": e_meta,
                "deploy_operations": o_meta
            }
        }
