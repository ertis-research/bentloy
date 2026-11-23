"""Deployment manifest builder for model-service runtime pods."""

from typing import Any

from ..domain.helpers import to_label_value
from ..domain.k8s_labels import (
    EXPERIMENT_LABEL,
    EXPERIMENT_NAME_ANNOTATION,
    MODEL_URI_ANNOTATION,
    OPERATION_ID_ANNOTATION,
    RUN_ID_LABEL,
)
from .template_loader import load_template_yaml


def build_model_metadata(
    model_slug: str,
    run_id: str = "",
    source_model_uri: str = "",
    experiment_name: str = "",
) -> tuple[dict[str, str], dict[str, str], dict[str, str]]:
    """Return (selector_labels, resource_labels, annotations) for a model-service resource.

    The selector stays at {app, model-slug}: a Deployment selector is immutable, so
    version labels must not be part of it. The version keys are always emitted (empty
    when unknown) so a merge-patch on redeploy overwrites values from a previous run.
    """

    selector_labels = {"app": "model-service", "model-slug": model_slug}
    resource_labels = {
        **selector_labels,
        RUN_ID_LABEL: run_id or "",
        EXPERIMENT_LABEL: to_label_value(experiment_name or ""),
    }
    annotations = {
        MODEL_URI_ANNOTATION: source_model_uri or "",
        EXPERIMENT_NAME_ANNOTATION: experiment_name or "",
    }
    return selector_labels, resource_labels, annotations


def build_deployment_manifest(
    namespace: str,
    model_name: str,
    model_slug: str,
    model_image: str,
    replicas: int,
    tracking_uri: str,
    model_uri: str,
    model_version: str,
    registry_secret: str,
    model_service_prefix: str,
    run_id: str = "",
    source_model_uri: str = "",
    experiment_name: str = "",
    operation_id: str = "",
) -> dict[str, Any]:
    """Render and customize a Deployment manifest for one model-service instance.

    `model_uri` is the path the container loads (MODEL_URI env); `source_model_uri`
    is the MLflow URI the image was built from (stored as an annotation).
    `operation_id` marks which deploy operation owns the resource (Deployment
    metadata only, not the pod template).
    """

    manifest = load_template_yaml("deployment.yaml")

    deploy_name = f"{model_service_prefix}-{model_slug}"
    selector_labels, labels, annotations = build_model_metadata(
        model_slug, run_id, source_model_uri, experiment_name
    )

    manifest["metadata"]["name"] = deploy_name
    manifest["metadata"]["namespace"] = namespace
    manifest["metadata"]["labels"] = labels
    manifest["metadata"]["annotations"] = {**annotations, OPERATION_ID_ANNOTATION: operation_id}

    spec = manifest["spec"]
    spec["replicas"] = replicas
    spec["selector"]["matchLabels"] = selector_labels
    spec["template"]["metadata"]["labels"] = dict(labels)
    spec["template"]["metadata"]["annotations"] = dict(annotations)

    pod_spec = spec["template"]["spec"]
    container = pod_spec["containers"][0]
    container["image"] = model_image
    container["env"] = [
        {"name": "MLFLOW_TRACKING_URI", "value": tracking_uri},
        {"name": "MODEL_NAME", "value": model_name},
        {"name": "MODEL_VERSION", "value": model_version},
        {"name": "MODEL_URI", "value": model_uri},
    ]

    if registry_secret and "/" in model_image:
        pod_spec["imagePullSecrets"] = [{"name": registry_secret}]
    else:
        pod_spec.pop("imagePullSecrets", None)

    return manifest
