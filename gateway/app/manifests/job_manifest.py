"""Job manifest builder for model-image build operations."""

from typing import Any

from .template_loader import load_template_text, load_template_yaml


def _find_named_item(items: list[dict[str, Any]], name: str) -> dict[str, Any]:
    """Return a named element from a list of template items."""

    for item in items:
        if item.get("name") == name:
            return item
    raise KeyError(f"Named item '{name}' not found in template.")


def build_job_manifest(
    job_name: str,
    namespace: str,
    model_name: str,
    model_version: str,
    model_uri: str,
    mlflow_tracking_uri: str,
    target_image: str,
    base_image: str,
    registry_secret: str,
    kaniko_image: str,
    insecure_registry_tls: bool,
    registry_server: str,
) -> dict[str, Any]:
    """Render and customize the Job manifest used to build model images."""

    manifest = load_template_yaml("job.yaml")
    prepare_script = load_template_text("prepare_build_context.sh")

    manifest["metadata"]["name"] = job_name
    manifest["metadata"]["namespace"] = namespace

    pod_spec = manifest["spec"]["template"]["spec"]
    pod_spec["imagePullSecrets"] = [{"name": registry_secret}]

    registry_creds_volume = _find_named_item(pod_spec["volumes"], "registry-creds")
    registry_creds_volume["secret"]["secretName"] = registry_secret

    init_container = _find_named_item(pod_spec["initContainers"], "prepare-build-context")
    init_container["env"] = [
        {"name": "MLFLOW_TRACKING_URI", "value": mlflow_tracking_uri},
        {"name": "MODEL_NAME", "value": model_name},
        {"name": "MODEL_VERSION", "value": model_version},
        {"name": "MODEL_URI", "value": model_uri},
    ]
    init_container["args"] = [prepare_script]

    kaniko_args = [
        "--dockerfile=/workspace/context/Dockerfile",
        "--context=dir:///workspace/context",
        f"--destination={target_image}",
        f"--build-arg=BASE_IMAGE={base_image}",
        "--snapshot-mode=redo",
        "--use-new-run",
    ]
    if insecure_registry_tls:
        kaniko_args.extend(["--skip-tls-verify", "--skip-tls-verify-pull"])
        if registry_server:
            kaniko_args.append(f"--skip-tls-verify-registry={registry_server}")

    kaniko_container = _find_named_item(pod_spec["containers"], "kaniko")
    kaniko_container["image"] = kaniko_image
    kaniko_container["args"] = kaniko_args

    return manifest
