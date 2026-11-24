"""Tests for the ``build_logs`` operation of LogsHandler."""

from collections.abc import Awaitable, Callable

import pytest

from gateway.app.handlers.logs_handler import LogsHandler, LogsRequestError
from tests.gateway.fakes import JOB, JOB_PATH, FakeCluster, make_pod

AddOperation = Callable[..., Awaitable[None]]


def build_pod(init_statuses: list[dict] | None = None, statuses: list[dict] | None = None) -> dict:
    """Build the running build pod with optional container statuses."""
    return make_pod("build-pod", "2026-01-01T00:00:00Z", statuses, init_statuses)


async def test_unknown_operation_is_404(logs_handler: LogsHandler) -> None:
    """An unknown operation id answers 404 naming the id."""
    with pytest.raises(LogsRequestError) as caught:
        await logs_handler.build_logs("nope")

    assert caught.value.status_code == 404
    assert "nope" in caught.value.message


async def test_queued_operation_has_no_job_yet(
    logs_handler: LogsHandler, logs_cluster: FakeCluster, add_build_operation: AddOperation
) -> None:
    """A queued operation has no Job yet and Kubernetes is not queried."""
    await add_build_operation(step="queued", status="queued")

    result = await logs_handler.build_logs("op_1")

    assert not result["available"]
    assert result["reason"] == "The build job has not been created yet."
    assert result["step"] == "queued"
    assert logs_cluster.requests == []


async def test_job_removed_after_ttl(
    logs_handler: LogsHandler, logs_cluster: FakeCluster, add_build_operation: AddOperation
) -> None:
    """A Job that no longer exists after its TTL is reported as removed."""
    del logs_cluster.resources[JOB_PATH]
    await add_build_operation()

    result = await logs_handler.build_logs("op_1")

    assert not result["available"]
    assert "no longer exists" in result["reason"]
    assert result["job_name"] == JOB


async def test_job_404_during_create_job_is_not_ttl(
    logs_handler: LogsHandler, logs_cluster: FakeCluster, add_build_operation: AddOperation
) -> None:
    """A missing Job during ``create_job`` means it is not created yet, not expired."""
    del logs_cluster.resources[JOB_PATH]
    await add_build_operation(step="create_job")

    result = await logs_handler.build_logs("op_1")

    assert not result["available"]
    assert result["reason"] == "The build job has not been created yet."


async def test_no_pod_yet(logs_handler: LogsHandler, add_build_operation: AddOperation) -> None:
    """A Job without pods reports that the pod has not been created."""
    await add_build_operation()

    result = await logs_handler.build_logs("op_1")

    assert not result["available"]
    assert result["reason"] == "The build pod has not been created yet."


async def test_both_containers_formatted_with_headers(
    logs_handler: LogsHandler, logs_cluster: FakeCluster, add_build_operation: AddOperation
) -> None:
    """Logs of both containers are joined under headers, with the requested tail."""
    logs_cluster.pods = [build_pod()]
    logs_cluster.logs = {"prepare-build-context": (200, "prepared\n"), "kaniko": (200, "pushed")}
    await add_build_operation()

    result = await logs_handler.build_logs("op_1", tail_lines=50)

    assert result["available"]
    assert result["reason"] is None
    assert result["pod_name"] == "build-pod"
    assert result["pod_phase"] == "Running"
    assert result["step"] == "kaniko_push"
    assert result["logs"] == (
        "===== prepare-build-context =====\nprepared\n\n===== kaniko =====\npushed\n"
    )
    containers = [r.url.params["container"] for r in logs_cluster.log_requests()]
    assert containers == ["prepare-build-context", "kaniko"]
    for request in logs_cluster.log_requests():
        assert request.url.params["tailLines"] == "50"
        assert "previous" not in request.url.params


async def test_init_done_and_kaniko_not_started(
    logs_handler: LogsHandler, logs_cluster: FakeCluster, add_build_operation: AddOperation
) -> None:
    """With init finished and Kaniko still waiting, the Kaniko section says it has not started."""
    logs_cluster.pods = [
        build_pod(statuses=[{"name": "kaniko", "state": {"waiting": {"reason": "PodInitializing"}}}])
    ]
    logs_cluster.logs = {"prepare-build-context": (200, "prepared\n"), "kaniko": (400, "waiting")}
    await add_build_operation()

    result = await logs_handler.build_logs("op_1")

    assert result["available"]
    assert result["logs"] == (
        "===== prepare-build-context =====\nprepared\n\n"
        "===== kaniko =====\n(not started yet: PodInitializing)\n"
    )


async def test_nothing_started_is_unavailable(
    logs_handler: LogsHandler, logs_cluster: FakeCluster, add_build_operation: AddOperation
) -> None:
    """When no container has started, logs are unavailable and the waiting reason is shown."""
    logs_cluster.pods = [
        build_pod(
            init_statuses=[
                {
                    "name": "prepare-build-context",
                    "state": {"waiting": {"reason": "PodInitializing"}},
                }
            ]
        )
    ]
    logs_cluster.logs = {"prepare-build-context": (400, "waiting"), "kaniko": (400, "waiting")}
    await add_build_operation()

    result = await logs_handler.build_logs("op_1")

    assert not result["available"]
    assert result["reason"] == "The build pod is starting. (PodInitializing)"
    assert result["logs"] == ""


async def test_container_log_404_marks_section(
    logs_handler: LogsHandler, logs_cluster: FakeCluster, add_build_operation: AddOperation
) -> None:
    """A container whose logs return 404 is marked in its section while the others are kept."""
    logs_cluster.pods = [build_pod()]
    logs_cluster.logs = {"kaniko": (200, "ok\n")}
    await add_build_operation()

    result = await logs_handler.build_logs("op_1")

    assert result["available"]
    assert "===== prepare-build-context =====\n(logs not available)\n" in result["logs"]


async def test_kubernetes_errors_are_502(
    logs_handler: LogsHandler, logs_cluster: FakeCluster, add_build_operation: AddOperation
) -> None:
    """A Kubernetes failure while reading container logs answers 502."""
    logs_cluster.pods = [build_pod()]
    logs_cluster.logs = {"prepare-build-context": (500, "boom")}
    await add_build_operation()

    with pytest.raises(LogsRequestError) as caught:
        await logs_handler.build_logs("op_1")

    assert caught.value.status_code == 502


@pytest.mark.parametrize("value", [0, 5001])
async def test_tail_lines_out_of_range_is_400(
    logs_handler: LogsHandler, add_build_operation: AddOperation, value: int
) -> None:
    """tail_lines outside 1..5000 answers 400."""
    await add_build_operation()

    with pytest.raises(LogsRequestError) as caught:
        await logs_handler.build_logs("op_1", value)

    assert caught.value.status_code == 400
