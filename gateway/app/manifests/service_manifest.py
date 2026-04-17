"""Service manifest builder for model-service network endpoints."""

from typing import Any

from .template_loader import load_template_yaml


def build_service_manifest(
    namespace: str,
    model_slug: str,
    model_service_prefix: str,
) -> dict[str, Any]:
    """Render and customize a Service manifest for one model-service instance."""

    manifest = load_template_yaml("service.yaml")

    service_name = f"{model_service_prefix}-{model_slug}"
    labels = {"app": "model-service", "model-slug": model_slug}

    manifest["metadata"]["name"] = service_name
    manifest["metadata"]["namespace"] = namespace
    manifest["metadata"]["labels"] = labels
    manifest["spec"]["selector"] = labels

    return manifest
