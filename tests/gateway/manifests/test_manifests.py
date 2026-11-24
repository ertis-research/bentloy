"""Tests for the Job, Deployment and Service manifest builders."""

from typing import Any

import pytest

from gateway.app.manifests.deployment_manifest import build_deployment_manifest
from gateway.app.manifests.job_manifest import build_job_manifest
from gateway.app.manifests.service_manifest import build_service_manifest
from gateway.app.manifests.template_loader import load_template_text

RUN_ID = "0123456789abcdef0123456789abcdef"


@pytest.fixture
def job_kwargs() -> dict[str, Any]:
    """Return the arguments of a plain build Job (no TLS flags, no cache, no extra pip index)."""
    return {
        "job_name": "build-model-tiny-demo",
        "namespace": "api-models",
        "model_name": "tiny_demo_model",
        "model_version": "latest",
        "model_uri": "",
        "mlflow_tracking_uri": "http://mlflow:5000",
        "target_image": "registry.example.com/api-models/model-tiny-demo:latest",
        "base_image": "registry.example.com/api-models/model-service-base:py310",
        "registry_secret": "model-registry-creds",
        "kaniko_image": "gcr.io/kaniko-project/executor:v1.23.2-debug",
        "insecure_registry_tls": False,
        "registry_server": "registry.example.com",
    }


@pytest.fixture
def deployment_kwargs() -> dict[str, Any]:
    """Return the arguments of a Deployment manifest without run metadata."""
    return {
        "namespace": "api-models",
        "model_name": "tiny_demo_model",
        "model_slug": "tiny-demo-model",
        "model_image": "registry.example.com/api-models/model-tiny-demo:latest",
        "replicas": 1,
        "tracking_uri": "http://mlflow:5000",
        "model_uri": "/models/model",
        "model_version": "image",
        "registry_secret": "model-registry-creds",
        "model_service_prefix": "model",
    }


def _kaniko(manifest: dict) -> dict:
    """Return the ``kaniko`` container of a build Job manifest."""
    containers = manifest["spec"]["template"]["spec"]["containers"]
    return next(c for c in containers if c["name"] == "kaniko")


def test_job_manifest_without_tls_flags(job_kwargs: dict[str, Any]) -> None:
    """A default build Job pushes without TLS flags, caching or an extra pip index."""
    manifest = build_job_manifest(**job_kwargs)

    assert manifest["metadata"]["name"] == "build-model-tiny-demo"
    assert manifest["metadata"]["namespace"] == "api-models"

    pod_spec = manifest["spec"]["template"]["spec"]
    assert pod_spec["imagePullSecrets"] == [{"name": "model-registry-creds"}]

    kaniko = _kaniko(manifest)
    assert (
        "--destination=registry.example.com/api-models/model-tiny-demo:latest" in kaniko["args"]
    )
    assert "--skip-tls-verify" not in kaniko["args"]
    # Defaults: no cache (single snapshot) and no extra pip index.
    assert "--single-snapshot" in kaniko["args"]
    assert not any(arg.startswith("--cache") for arg in kaniko["args"])
    assert not any("MODEL_PIP_EXTRA_INDEX_URL" in arg for arg in kaniko["args"])


def test_job_manifest_with_cache_and_pip_index(job_kwargs: dict[str, Any]) -> None:
    """Enabling the cache and a pip index adds the matching Kaniko arguments."""
    job_kwargs.update(
        target_image="registry.example.com:5000/api-models/model-tiny-demo:v1",
        base_image="registry.example.com/api-models/model-service-base:py312",
        pip_extra_index_url="https://download.pytorch.org/whl/cpu",
        kaniko_cache=True,
    )

    kaniko = _kaniko(build_job_manifest(**job_kwargs))

    assert "--cache=true" in kaniko["args"]
    assert "--cache-repo=registry.example.com:5000/api-models/kaniko-cache" in kaniko["args"]
    assert "--single-snapshot" not in kaniko["args"]
    assert (
        "--build-arg=MODEL_PIP_EXTRA_INDEX_URL=https://download.pytorch.org/whl/cpu"
        in kaniko["args"]
    )


def test_build_context_installs_dependencies_before_copying_the_model() -> None:
    """The build context script installs dependencies before copying the model (layer caching)."""
    script = load_template_text("prepare_build_context.sh")
    dockerfile = script[script.index("ARG BASE_IMAGE"):script.index('"""\n)')]
    run_pip = dockerfile.index("RUN if [ -s /tmp/extra_requirements.txt ]")
    assert run_pip < dockerfile.index("COPY model /models/model")
    assert "ARG MODEL_PIP_EXTRA_INDEX_URL" in dockerfile
    assert (
        '${MODEL_PIP_EXTRA_INDEX_URL:+--extra-index-url "$MODEL_PIP_EXTRA_INDEX_URL"}' in dockerfile
    )


def test_kaniko_cache_repo_without_namespace(job_kwargs: dict[str, Any]) -> None:
    """Without a registry namespace the cache repo sits directly under the registry."""
    job_kwargs.update(
        target_image="registry.local:5000/model-tiny-demo:v1",
        base_image="registry.local:5000/model-service-base:py312",
        registry_server="registry.local:5000",
        kaniko_cache=True,
    )

    kaniko = _kaniko(build_job_manifest(**job_kwargs))

    assert "--cache-repo=registry.local:5000/kaniko-cache" in kaniko["args"]


def test_job_manifest_with_tls_flags(job_kwargs: dict[str, Any]) -> None:
    """An insecure registry adds the skip-TLS flags, including the per-registry one."""
    job_kwargs["insecure_registry_tls"] = True

    kaniko = _kaniko(build_job_manifest(**job_kwargs))

    assert "--skip-tls-verify" in kaniko["args"]
    assert "--skip-tls-verify-pull" in kaniko["args"]
    assert "--skip-tls-verify-registry=registry.example.com" in kaniko["args"]


def test_deployment_manifest(deployment_kwargs: dict[str, Any]) -> None:
    """The Deployment is named after the prefix and slug and uses the registry pull secret."""
    deployment_kwargs["replicas"] = 2

    manifest = build_deployment_manifest(**deployment_kwargs)

    assert manifest["metadata"]["name"] == "model-tiny-demo-model"
    assert manifest["metadata"]["namespace"] == "api-models"
    assert manifest["spec"]["replicas"] == 2
    pod_spec = manifest["spec"]["template"]["spec"]
    assert pod_spec["imagePullSecrets"] == [{"name": "model-registry-creds"}]


def test_service_manifest() -> None:
    """The Service is named after the prefix and slug and selects the model pods."""
    manifest = build_service_manifest(
        namespace="api-models",
        model_slug="tiny-demo-model",
        model_service_prefix="model",
    )
    assert manifest["metadata"]["name"] == "model-tiny-demo-model"
    assert manifest["metadata"]["namespace"] == "api-models"
    assert manifest["spec"]["selector"] == {"app": "model-service", "model-slug": "tiny-demo-model"}


def test_deployment_manifest_records_run_and_experiment(deployment_kwargs: dict[str, Any]) -> None:
    """Run and experiment metadata go to labels and annotations, never to the selector."""
    manifest = build_deployment_manifest(
        **deployment_kwargs,
        run_id=RUN_ID,
        source_model_uri=f"runs:/{RUN_ID}/ludwig-model",
        experiment_name="Churn Prediction / EU",
        operation_id="op_20261123101500_a1b2c3",
    )
    selector = {"app": "model-service", "model-slug": "tiny-demo-model"}
    expected_labels = {
        **selector,
        "bentloy.io/run-id": RUN_ID,
        "bentloy.io/experiment": "churn-prediction-eu",
    }
    expected_annotations = {
        "bentloy.io/model-uri": f"runs:/{RUN_ID}/ludwig-model",
        "bentloy.io/experiment-name": "Churn Prediction / EU",
    }
    # Version metadata on the resource and the pods, never in the immutable selector.
    assert manifest["metadata"]["labels"] == expected_labels
    # The owning operation is on the Deployment only: a pod-template change would
    # restart the pods.
    assert manifest["metadata"]["annotations"] == {
        **expected_annotations,
        "bentloy.io/operation-id": "op_20261123101500_a1b2c3",
    }
    assert manifest["spec"]["selector"]["matchLabels"] == selector
    assert manifest["spec"]["template"]["metadata"]["labels"] == expected_labels
    assert manifest["spec"]["template"]["metadata"]["annotations"] == expected_annotations
    # The container still loads the model baked into the image.
    env = {e["name"]: e["value"] for e in manifest["spec"]["template"]["spec"]["containers"][0]["env"]}
    assert env["MODEL_URI"] == "/models/model"


def test_service_manifest_records_run_and_experiment() -> None:
    """The Service records the run, experiment and operation; labels are length-limited."""
    manifest = build_service_manifest(
        namespace="api-models",
        model_slug="tiny-demo-model",
        model_service_prefix="model",
        run_id=RUN_ID,
        source_model_uri=f"runs:/{RUN_ID}/ludwig-model",
        experiment_name="X" * 80,
        operation_id="op_1",
    )
    labels = manifest["metadata"]["labels"]
    assert labels["bentloy.io/run-id"] == RUN_ID
    assert labels["bentloy.io/experiment"] == "x" * 63
    assert manifest["metadata"]["annotations"]["bentloy.io/experiment-name"] == "X" * 80
    assert manifest["metadata"]["annotations"]["bentloy.io/operation-id"] == "op_1"
    assert manifest["spec"]["selector"] == {"app": "model-service", "model-slug": "tiny-demo-model"}


def test_metadata_keys_are_always_present() -> None:
    """Empty metadata values are written so a merge patch clears a previous run's labels."""
    manifest = build_service_manifest(
        namespace="api-models",
        model_slug="tiny-demo-model",
        model_service_prefix="model",
    )
    assert manifest["metadata"]["labels"]["bentloy.io/run-id"] == ""
    assert manifest["metadata"]["labels"]["bentloy.io/experiment"] == ""
    assert manifest["metadata"]["annotations"] == {
        "bentloy.io/model-uri": "",
        "bentloy.io/experiment-name": "",
        "bentloy.io/operation-id": "",
    }
