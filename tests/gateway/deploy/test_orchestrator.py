"""Tests for DeployOrchestrator: running operations and finding model resources."""

from collections.abc import Callable

import httpx
import pytest

from gateway.app.deploy.operation_store import DeployOperationStore
from gateway.app.deploy.orchestrator import DeployOrchestrator
from tests.gateway.fakes import (
    JOBS,
    RUN_ID,
    FakeCluster,
    FakeKubernetesApiClient,
    build_operation,
    build_request,
)

OrchestratorFactory = Callable[..., DeployOrchestrator]


@pytest.fixture
def strict_cluster(k8s_cluster: FakeCluster) -> FakeCluster:
    """Make ``k8s_cluster`` strict: any request without a route fails the test."""
    k8s_cluster.strict = True
    return k8s_cluster


def _job_manifest(**kwargs) -> dict:
    """Return a minimal Job manifest named after ``job_name``."""
    return {"metadata": {"name": kwargs["job_name"]}}


@pytest.mark.usefixtures("strict_cluster")
async def test_run_operation_ready(
    make_orchestrator: OrchestratorFactory,
    k8s_cluster: FakeCluster,
    operation_store: DeployOperationStore,
) -> None:
    """A successful build and rollout marks the operation ready and passes run metadata to manifests."""
    await operation_store.add(build_operation("op_ready", build_request()))
    fake_k8s = FakeKubernetesApiClient()
    builder_calls: dict[str, dict] = {}
    job_path = f"{JOBS}/build-model-tiny-demo-abc123"
    k8s_cluster.routes = {
        ("POST", JOBS): httpx.Response(status_code=201, json={"kind": "Job"}),
        ("GET", job_path): httpx.Response(status_code=200, json={"status": {"succeeded": 1}}),
    }
    orchestrator = make_orchestrator(
        k8s_client=fake_k8s,
        resolve_k8s_auth=lambda: (
            "https://kubernetes.default.svc",
            {"Authorization": "Bearer test"},
            False,
        ),
        build_job_manifest=_job_manifest,
        build_delete_job_manifest=_job_manifest,
        build_deployment_manifest=lambda **kwargs: builder_calls.setdefault("deployment", kwargs),
        build_service_manifest=lambda **kwargs: builder_calls.setdefault("service", kwargs),
        kaniko_image="gcr.io/kaniko-project/executor:v1.23.2-debug",
        model_service_prefix="model",
        build_timeout_sec=10,
        rollout_timeout_sec=10,
    )

    await orchestrator.run_operation("op_ready")

    result = await operation_store.get("op_ready")
    assert result is not None
    assert result["status"] == "ready"
    assert result["step"] == "completed"
    assert result["message"] == "Deployment completed successfully."
    assert len(fake_k8s.ensure_calls) == 2
    assert len(fake_k8s.wait_calls) == 1
    # Only the Job creation and the Job status polling reach the Kubernetes API.
    k8s_cluster.assert_no_unexpected()
    # The container loads the model baked into the image; the MLflow URI goes to metadata.
    assert builder_calls["deployment"]["model_uri"] == "/models/model"
    for kind in ("deployment", "service"):
        assert builder_calls[kind]["run_id"] == RUN_ID
        assert builder_calls[kind]["source_model_uri"] == f"runs:/{RUN_ID}/ludwig-model"
        assert builder_calls[kind]["experiment_name"] == "Tiny Demo"


@pytest.mark.usefixtures("strict_cluster")
async def test_run_operation_job_creation_error_marks_failed(
    make_orchestrator: OrchestratorFactory,
    k8s_cluster: FakeCluster,
    operation_store: DeployOperationStore,
) -> None:
    """A failed Job creation marks the operation failed with the status code and response body."""
    await operation_store.add(
        build_operation("op_failed", build_request(job_name="build-model-fail-abc123"))
    )
    k8s_cluster.routes = {("POST", JOBS): httpx.Response(status_code=500, text="boom")}
    orchestrator = make_orchestrator(
        k8s_client=FakeKubernetesApiClient(),
        resolve_k8s_auth=lambda: (
            "https://kubernetes.default.svc",
            {"Authorization": "Bearer test"},
            False,
        ),
        build_job_manifest=_job_manifest,
        build_delete_job_manifest=_job_manifest,
        build_deployment_manifest=lambda **kwargs: {
            "metadata": {"name": f"model-{kwargs['model_slug']}"}
        },
        build_service_manifest=lambda **kwargs: {
            "metadata": {"name": f"model-{kwargs['model_slug']}"}
        },
        kaniko_image="gcr.io/kaniko-project/executor:v1.23.2-debug",
        model_service_prefix="model",
        build_timeout_sec=10,
        rollout_timeout_sec=10,
    )

    await orchestrator.run_operation("op_failed")

    result = await operation_store.get("op_failed")
    assert result is not None
    assert result["status"] == "failed"
    assert result["step"] == "failed"
    assert result["error"] is not None
    assert "Job creation failed (500)" in (result["error"] or "")
    assert result["error_log"] == "boom"
    k8s_cluster.assert_no_unexpected()


@pytest.mark.usefixtures("strict_cluster")
async def test_run_operation_build_failure_splits_reason_and_log(
    make_orchestrator: OrchestratorFactory,
    k8s_cluster: FakeCluster,
    operation_store: DeployOperationStore,
) -> None:
    """A failed build keeps a short reason in ``error`` and the full container logs in ``error_log``."""

    class LogsK8s(FakeKubernetesApiClient):
        """Kubernetes client double returning push-denied Kaniko logs."""

        async def read_job_logs(self, client, base_url, headers, namespace, job_name, container):
            """Return Kaniko logs with a denied push, or the prepare container logs."""
            if container == "kaniko":
                return (
                    "\x1b[36mINFO\x1b[0m[0000] Retrieving image manifest\n"
                    "error checking push permissions: UNAUTHORIZED: "
                    "unauthorized to access repository\n"
                )
            return "Model downloaded.\n"

    await operation_store.add(
        build_operation("op_push", build_request(job_name="build-model-push-abc123"))
    )
    k8s_cluster.routes = {
        ("POST", JOBS): httpx.Response(status_code=201, json={"kind": "Job"}),
        ("GET", f"{JOBS}/build-model-push-abc123"): httpx.Response(
            status_code=200, json={"status": {"failed": 1}}
        ),
    }
    orchestrator = make_orchestrator(
        k8s_client=LogsK8s(),
        build_job_manifest=_job_manifest,
        kaniko_image="kaniko",
        model_service_prefix="model",
        build_timeout_sec=10,
        rollout_timeout_sec=10,
    )

    await orchestrator.run_operation("op_push")

    result = await operation_store.get("op_push")
    assert result is not None
    assert result["status"] == "failed"
    assert "Registry push denied" in (result["error"] or "")
    assert "model-registry-creds" in (result["error"] or "")
    assert "Retrieving image manifest" not in (result["error"] or "")
    assert "Retrieving image manifest" in (result["error_log"] or "")
    assert "[prepare-build-context]\nModel downloaded." in (result["error_log"] or "")

    k8s_cluster.assert_no_unexpected()
    summaries = await operation_store.list_summaries()
    assert "error_log" not in summaries[0]


@pytest.fixture
def model_paths() -> dict[str, str]:
    """Return the Kubernetes API paths of the Service and Deployment of ``bentloy-model-x``."""
    return {
        "service": "/api/v1/namespaces/ns/services/bentloy-model-x",
        "deployment": "/apis/apps/v1/namespaces/ns/deployments/bentloy-model-x",
    }


async def test_find_model_resource_returns_none_after_checking_service_then_deployment(
    make_orchestrator: OrchestratorFactory, k8s_cluster: FakeCluster, model_paths: dict[str, str]
) -> None:
    """Without either resource, the Service is looked up first and then the Deployment."""
    orchestrator = make_orchestrator()

    assert await orchestrator.find_model_resource("ns", "bentloy-model-x") is None
    assert k8s_cluster.requested_paths == [model_paths["service"], model_paths["deployment"]]


async def test_find_model_resource_falls_back_to_the_deployment(
    make_orchestrator: OrchestratorFactory, k8s_cluster: FakeCluster, model_paths: dict[str, str]
) -> None:
    """When the Service is missing, the Deployment metadata is returned."""
    metadata = {"name": "bentloy-model-x", "labels": {"bentloy.io/run-id": RUN_ID}}
    k8s_cluster.resources[model_paths["deployment"]] = {"metadata": metadata}

    found = await make_orchestrator().find_model_resource("ns", "bentloy-model-x")

    assert found == metadata


async def test_find_model_resource_raises_when_kubernetes_denies_access(
    make_orchestrator: OrchestratorFactory, k8s_cluster: FakeCluster, model_paths: dict[str, str]
) -> None:
    """An API error other than 404 is raised instead of being treated as missing."""
    k8s_cluster.routes[("GET", model_paths["service"])] = httpx.Response(403, text="forbidden")

    with pytest.raises(RuntimeError, match="403"):
        await make_orchestrator().find_model_resource("ns", "bentloy-model-x")
