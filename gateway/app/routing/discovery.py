"""Kubernetes service discovery for dynamically deployed model endpoints."""

from typing import Any, Callable

import httpx

from ..domain.helpers import slug_to_model_name
from ..domain.k8s_labels import (
    EXPERIMENT_NAME_ANNOTATION,
    MODEL_URI_ANNOTATION,
    RUN_ID_LABEL,
)


class KubernetesModelDiscovery:
    """Discover model services that follow the configured naming convention."""

    def __init__(
        self,
        model_service_prefix: str,
        model_namespace: str,
        model_url_template: str,
        client_factory: Callable[..., httpx.AsyncClient] = httpx.AsyncClient,
    ) -> None:
        self.model_service_prefix = model_service_prefix
        self.model_namespace = model_namespace
        self.model_url_template = model_url_template
        self.client_factory = client_factory

    async def discover_services(
        self,
        base_url: str,
        headers: dict[str, str],
        verify: str | bool,
    ) -> tuple[list[dict[str, str | None]], str | None]:
        """Return discovered model services and an optional discovery error message."""

        url = f"{base_url}/api/v1/namespaces/{self.model_namespace}/services"

        try:
            async with self.client_factory(timeout=5.0, verify=verify) as client:
                response = await client.get(url, headers=headers)
                response.raise_for_status()
                items = response.json().get("items", [])
        except Exception as exc:
            return [], f"Could not discover Kubernetes services: {exc}"

        prefix = f"{self.model_service_prefix}-"
        discovered: list[dict[str, str | None]] = []

        for item in items:
            metadata = item.get("metadata") or {}
            service_name = metadata.get("name", "")
            if not service_name.startswith(prefix):
                continue
            labels = metadata.get("labels") or {}
            annotations = metadata.get("annotations") or {}

            model_slug = service_name[len(prefix) :]
            inferred_model_name = slug_to_model_name(model_slug)
            model_name = inferred_model_name
            base = self.model_url_template.format(
                service_name=service_name,
                model_name=model_name,
                model_slug=model_slug,
                namespace=self.model_namespace,
            )
            discovered.append(
                {
                    "service_name": service_name,
                    "model_slug": model_slug,
                    "inferred_model_name": inferred_model_name,
                    "base_url": base,
                    "predict_endpoint": f"{base}/predict",
                    # Empty/missing on services deployed before these keys existed.
                    "run_id": labels.get(RUN_ID_LABEL) or None,
                    "model_uri": annotations.get(MODEL_URI_ANNOTATION) or None,
                    "experiment_name": annotations.get(EXPERIMENT_NAME_ANNOTATION) or None,
                    # RFC 3339 UTC timestamp set by Kubernetes; sorts lexicographically.
                    "created_at": metadata.get("creationTimestamp") or None,
                }
            )

        return sort_newest_first(discovered), None


def sort_newest_first(services: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Sort services from newest to oldest; ties (or missing dates) go by name."""

    by_name = sorted(services, key=lambda service: service.get("service_name") or "")
    return sorted(by_name, key=lambda service: service.get("created_at") or "", reverse=True)
