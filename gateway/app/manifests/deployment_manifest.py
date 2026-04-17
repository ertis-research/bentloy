"""Deployment manifest builder for model-service runtime pods."""

from typing import Any

from .template_loader import load_template_yaml


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
) -> dict[str, Any]:
    """Render and customize a Deployment manifest for one model-service instance."""

    manifest = load_template_yaml("deployment.yaml")

    deploy_name = f"{model_service_prefix}-{model_slug}"
    labels = {"app": "model-service", "model-slug": model_slug}

    manifest["metadata"]["name"] = deploy_name
    manifest["metadata"]["namespace"] = namespace
    manifest["metadata"]["labels"] = labels

    spec = manifest["spec"]
    spec["replicas"] = replicas
    spec["selector"]["matchLabels"] = labels
    spec["template"]["metadata"]["labels"] = labels

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
