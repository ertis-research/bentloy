"""Tests for the ``deployment_logs`` operation of LogsHandler."""

import httpx
import pytest

from gateway.app.handlers.logs_handler import LogsHandler, LogsRequestError
from tests.gateway.fakes import DEPLOYMENT_PATH, PODS, PREFIX, SERVICE, FakeCluster, make_pod

CREATED = "2026-01-01T00:00:00Z"


def model_status(ready: bool = True, restarts: int = 0, waiting: str | None = None) -> dict:
    """Build the container status of the ``model-service`` container."""
    state = {"waiting": {"reason": waiting}} if waiting else {"running": {}}
    return {"name": "model-service", "ready": ready, "restartCount": restarts, "state": state}


async def test_happy_path_picks_newest_non_terminating_pod(
    logs_handler: LogsHandler, logs_cluster: FakeCluster
) -> None:
    """The newest pod that is not terminating is chosen and its logs are returned."""
    logs_cluster.pods = [
        make_pod("old", "2026-01-01T00:00:00Z", [model_status()]),
        make_pod("terminating", "2026-01-03T00:00:00Z", [model_status()], deleting=True),
        make_pod("new", "2026-01-02T00:00:00Z", [model_status(True, 2)]),
    ]
    logs_cluster.logs["model-service"] = (200, "hello\n")

    result = await logs_handler.deployment_logs(SERVICE, tail_lines=100)

    assert result == {
        "service_name": SERVICE,
        "pod_name": "new",
        "pod_phase": "Running",
        "container_ready": True,
        "restart_count": 2,
        "available": True,
        "reason": None,
        "logs": "hello\n",
    }
    pods_request = next(r for r in logs_cluster.requests if r.url.path == PODS)
    assert pods_request.url.params["labelSelector"] == "app=model-service,model-slug=churn-v2"
    (log_request,) = logs_cluster.log_requests()
    assert log_request.url.path == f"{PODS}/new/log"
    params = log_request.url.params
    assert params["container"] == "model-service"
    assert params["tailLines"] == "100"
    assert params["timestamps"] == "false"
    assert "previous" not in params


async def test_previous_param_only_when_set(logs_handler: LogsHandler, logs_cluster: FakeCluster) -> None:
    """The ``previous`` query parameter is sent only when requested."""
    logs_cluster.pods = [make_pod("p", CREATED, [model_status(True, 1)])]
    logs_cluster.logs["model-service"] = (200, "old run")

    result = await logs_handler.deployment_logs(SERVICE, previous=True)

    assert result["available"]
    assert logs_cluster.log_requests()[0].url.params["previous"] == "true"


@pytest.mark.parametrize("name", ["gateway", PREFIX, f"{PREFIX}-", "other-churn"])
async def test_unknown_or_invalid_service_name_is_404(
    logs_handler: LogsHandler, logs_cluster: FakeCluster, name: str
) -> None:
    """Names outside the model prefix answer 404 without calling Kubernetes."""
    with pytest.raises(LogsRequestError) as caught:
        await logs_handler.deployment_logs(name)

    assert caught.value.status_code == 404, name
    assert logs_cluster.requests == []


async def test_missing_deployment_is_404(logs_handler: LogsHandler, logs_cluster: FakeCluster) -> None:
    """A model without a Deployment answers 404 naming the service."""
    del logs_cluster.resources[DEPLOYMENT_PATH]

    with pytest.raises(LogsRequestError) as caught:
        await logs_handler.deployment_logs(SERVICE)

    assert caught.value.status_code == 404
    assert SERVICE in caught.value.message


async def test_scaled_to_zero(logs_handler: LogsHandler, logs_cluster: FakeCluster) -> None:
    """A Deployment scaled to zero reports logs as unavailable."""
    logs_cluster.resources[DEPLOYMENT_PATH] = {"spec": {"replicas": 0}}

    result = await logs_handler.deployment_logs(SERVICE)

    assert not result["available"]
    assert result["reason"] == "The deployment is scaled to 0 replicas."
    assert result["pod_name"] is None
    assert result["logs"] == ""
    assert result["restart_count"] == 0


async def test_no_pods(logs_handler: LogsHandler, logs_cluster: FakeCluster) -> None:
    """When only terminating pods exist, no running pod is reported."""
    logs_cluster.pods = [make_pod("gone", CREATED, deleting=True)]

    result = await logs_handler.deployment_logs(SERVICE)

    assert not result["available"]
    assert result["reason"] == "No running pod for this model yet."
    assert result["pod_name"] is None


async def test_container_not_started_reports_starting_with_waiting_reason(
    logs_handler: LogsHandler, logs_cluster: FakeCluster
) -> None:
    """A container that has not started reports the waiting reason."""
    logs_cluster.pods = [make_pod("p", CREATED, [model_status(False, 0, "ContainerCreating")])]
    logs_cluster.logs["model-service"] = (400, "container is waiting to start")

    result = await logs_handler.deployment_logs(SERVICE)

    assert not result["available"]
    assert result["reason"] == "The model pod is starting. (ContainerCreating)"
    assert result["pod_name"] == "p"
    assert not result["container_ready"]
    assert result["logs"] == ""


async def test_previous_without_restart(logs_handler: LogsHandler, logs_cluster: FakeCluster) -> None:
    """Asking for previous logs of a pod that never restarted explains there is no previous container."""
    logs_cluster.pods = [make_pod("p", CREATED, [model_status()])]
    logs_cluster.logs["model-service"] = (400, "previous terminated container not found")

    result = await logs_handler.deployment_logs(SERVICE, previous=True)

    assert not result["available"]
    assert "No previous container" in result["reason"]


async def test_previous_after_restart_without_logs(
    logs_handler: LogsHandler, logs_cluster: FakeCluster
) -> None:
    """After a restart without readable previous logs the reason says so."""
    logs_cluster.pods = [make_pod("p", CREATED, [model_status(True, 3)])]
    logs_cluster.logs["model-service"] = (400, "previous terminated container not found")

    result = await logs_handler.deployment_logs(SERVICE, previous=True)

    assert not result["available"]
    assert result["reason"] == "The previous container's logs are not available."


async def test_pod_log_404(logs_handler: LogsHandler, logs_cluster: FakeCluster) -> None:
    """A pod that disappears between the listing and the log request is reported as gone."""
    logs_cluster.pods = [make_pod("p", CREATED, [model_status()])]

    result = await logs_handler.deployment_logs(SERVICE)

    assert not result["available"]
    assert result["reason"] == "The model pod no longer exists."


@pytest.mark.parametrize(
    ("pods_status", "message_part"),
    [(200, "500"), (403, None)],
    ids=["log-request-500", "pod-listing-403"],
)
async def test_kubernetes_errors_are_502(
    logs_handler: LogsHandler,
    logs_cluster: FakeCluster,
    pods_status: int,
    message_part: str | None,
) -> None:
    """Kubernetes API failures answer 502."""
    logs_cluster.pods = [make_pod("p", CREATED, [model_status()])]
    logs_cluster.logs["model-service"] = (500, "boom")
    logs_cluster.pods_status = pods_status

    with pytest.raises(LogsRequestError) as caught:
        await logs_handler.deployment_logs(SERVICE)

    assert caught.value.status_code == 502
    if message_part is not None:
        assert message_part in caught.value.message


async def test_transport_error_is_502(logs_handler: LogsHandler) -> None:
    """A connection failure to the Kubernetes API answers 502."""

    def failing(request: httpx.Request) -> httpx.Response:
        """Simulate an unreachable API server."""
        raise httpx.ConnectError("down")

    logs_handler.http_client_factory = lambda verify: httpx.AsyncClient(
        transport=httpx.MockTransport(failing)
    )

    with pytest.raises(LogsRequestError) as caught:
        await logs_handler.deployment_logs(SERVICE)

    assert caught.value.status_code == 502


async def test_auth_failure_is_502(logs_handler: LogsHandler) -> None:
    """Failing to resolve the in-cluster credentials answers 502."""

    def no_auth():
        """Simulate running outside the cluster."""
        raise RuntimeError("not in cluster")

    logs_handler.resolve_k8s_auth = no_auth

    with pytest.raises(LogsRequestError) as caught:
        await logs_handler.deployment_logs(SERVICE)

    assert caught.value.status_code == 502


@pytest.mark.parametrize("value", [0, 5001, -1])
async def test_tail_lines_out_of_range_is_400(logs_handler: LogsHandler, value: int) -> None:
    """tail_lines outside 1..5000 answers 400."""
    with pytest.raises(LogsRequestError) as caught:
        await logs_handler.deployment_logs(SERVICE, tail_lines=value)

    assert caught.value.status_code == 400
    assert caught.value.message == "tail_lines must be between 1 and 5000."


@pytest.mark.parametrize("value", [1, 5000])
async def test_tail_lines_bounds_are_accepted(logs_handler: LogsHandler, value: int) -> None:
    """The lower and upper tail_lines bounds are valid."""
    await logs_handler.deployment_logs(SERVICE, tail_lines=value)
