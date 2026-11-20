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
    pip_extra_index_url: str = "",
    kaniko_cache: bool = False,
) -> dict[str, Any]:
    """Render and customize the Job manifest used to build model images.

    `pip_extra_index_url` is passed to the model's `pip install` (e.g. PyTorch CPU
    wheels). With `kaniko_cache`, layers are cached in `<registry>[/<namespace>]/kaniko-cache`
    (next to the target image), so the dependency layer is reused across builds.
    """

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
        "--compressed-caching=false",
    ]
    if pip_extra_index_url:
        kaniko_args.append(f"--build-arg=MODEL_PIP_EXTRA_INDEX_URL={pip_extra_index_url}")
    if kaniko_cache:
        # Layer caching needs one layer per instruction, so no --single-snapshot here.
        cache_repo = f"{target_image.rsplit('/', 1)[0]}/kaniko-cache"
        kaniko_args.extend(["--cache=true", f"--cache-repo={cache_repo}"])
    else:
        kaniko_args.append("--single-snapshot")
    if insecure_registry_tls:
        kaniko_args.extend(["--skip-tls-verify", "--skip-tls-verify-pull"])
        if registry_server:
            kaniko_args.append(f"--skip-tls-verify-registry={registry_server}")

    kaniko_container = _find_named_item(pod_spec["containers"], "kaniko")
    kaniko_container["image"] = kaniko_image
    kaniko_container["args"] = kaniko_args

    return manifest

def build_delete_job_manifest(
    job_name: str,
    namespace: str,
    model_slug: str,
    image_ref: str,
    registry_secret: str,
    insecure_registry_tls: bool = False,
) -> dict[str, Any]:
    """Render and customize the Job manifest used to delete model images via Crane."""
    
    manifest = load_template_yaml("delete-job.yaml")

    # Update metadata
    manifest["metadata"]["name"] = job_name
    manifest["metadata"]["namespace"] = namespace
    manifest["metadata"]["labels"]["model-slug"] = model_slug

    # Update pod template metadata
    manifest["spec"]["template"]["metadata"]["labels"]["model-slug"] = model_slug

    pod_spec = manifest["spec"]["template"]["spec"]
    
    # Update registry secret
    pod_spec["imagePullSecrets"] = [{"name": registry_secret}]
    registry_creds_volume = _find_named_item(pod_spec["volumes"], "registry-creds")
    registry_creds_volume["secret"]["secretName"] = registry_secret

    crane_container = _find_named_item(pod_spec["containers"], "crane")
    
    # ---------------------------------------------------------
    # NEW LOGIC: Resolve the digest before deleting
    # ---------------------------------------------------------
    crane_script = """
    set -e
    IMAGE="$1"
    shift
    OPTS="$@"
    
    echo "Target Image: $IMAGE"
    
    # If the image already contains '@', it's already a digest
    if echo "$IMAGE" | grep -q '@'; then
        crane delete $OPTS "$IMAGE"
    else
        # 1. Fetch the exact sha256 digest from the registry
        DIGEST=$(crane digest $OPTS "$IMAGE")
        echo "Resolved digest: $DIGEST"
        
        # 2. Extract base image (removes the tag, safely ignores port colons)
        BASE_IMAGE="${IMAGE%:*}"
        
        # 3. Delete using the base image + digest
        echo "Executing: crane delete $OPTS ${BASE_IMAGE}@${DIGEST}"
        crane delete $OPTS "${BASE_IMAGE}@${DIGEST}"
    fi
    """

    # We use 'sh -c' to run the script. The '--' assigns arguments correctly 
    # so $1 is the image_ref, and the rest ($@) are the optional crane flags.
    crane_command = ["sh", "-c", crane_script, "--", image_ref]
    
    if insecure_registry_tls:
        crane_command.append("--insecure")
        
    crane_container["command"] = crane_command

    return manifest