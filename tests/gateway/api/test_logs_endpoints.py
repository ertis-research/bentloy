"""Tests for the ``/deployment_logs`` and ``/build_logs`` endpoints at the GatewayService level."""

from gateway.app.handlers.logs_handler import LogsHandler
from gateway.service import GatewayService
from tests.gateway.fakes import DEPLOYMENT_PATH, SERVICE, FakeCluster, FakeContext


async def test_errors_set_status_code_and_body(gateway_service: GatewayService) -> None:
    """Request errors set the HTTP status on the context and return ``error`` and ``detail``."""
    ctx = FakeContext()
    body = await gateway_service.build_logs("missing", ctx=ctx)
    assert ctx.response.status_code == 404
    assert body["error"] == body["detail"]
    assert "missing" in body["detail"]

    ctx = FakeContext()
    body = await gateway_service.deployment_logs("bentloy-model-x", tail_lines=0, ctx=ctx)
    assert ctx.response.status_code == 400
    assert body["detail"] == "tail_lines must be between 1 and 5000."

    ctx = FakeContext()
    body = await gateway_service.deployment_logs("unrelated", ctx=ctx)
    assert ctx.response.status_code == 404


async def test_success_keeps_status_200(
    gateway_service: GatewayService, logs_handler: LogsHandler, logs_cluster: FakeCluster
) -> None:
    """Expected states such as a scaled-to-zero Deployment keep HTTP 200."""
    logs_cluster.resources[DEPLOYMENT_PATH] = {"spec": {"replicas": 0}}
    gateway_service.logs_handler = logs_handler
    ctx = FakeContext()

    body = await gateway_service.deployment_logs(SERVICE, ctx=ctx)

    assert ctx.response.status_code == 200
    assert not body["available"]
