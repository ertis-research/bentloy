"""Service manifest builder for model-service network endpoints."""

from typing import Any

from ..domain.k8s_labels import OPERATION_ID_ANNOTATION
from .deployment_manifest import build_model_metadata
from .template_loader import load_template_yaml


def build_service_manifest(
    namespace: str,
    model_slug: str,
    model_service_prefix: str,
    run_id: str = "",
    source_model_uri: str = "",
    experiment_name: str = "",
    operation_id: str = "",
) -> dict[str, Any]:
    """Render and customize a Service manifest for one model-service instance."""

    manifest = load_template_yaml("service.yaml")

    service_name = f"{model_service_prefix}-{model_slug}"
    selector_labels, labels, annotations = build_model_metadata(
        model_slug, run_id, source_model_uri, experiment_name
    )

    manifest["metadata"]["name"] = service_name
    manifest["metadata"]["namespace"] = namespace
    manifest["metadata"]["labels"] = labels
    manifest["metadata"]["annotations"] = {**annotations, OPERATION_ID_ANNOTATION: operation_id}
    manifest["spec"]["selector"] = selector_labels

    return manifest
