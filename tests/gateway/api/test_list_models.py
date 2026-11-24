"""Tests for the ``/list_models`` endpoint at the GatewayService level."""

import pytest

from gateway import service as gateway_entry
from gateway.service import GatewayService
from tests.gateway.fakes import FakeContext


async def test_list_models_surfaces_discovery_error() -> None:
    """A discovery failure is reported in the response instead of failing the request."""
    gateway = gateway_entry.GatewayService.inner()

    async def fake_discovery():
        """Simulate a failing Kubernetes discovery."""
        return [], "simulated discovery error"

    gateway.info_handler.discover_k8s_model_services = fake_discovery  # type: ignore[method-assign]
    result = await gateway.list_models()

    assert result["discovery"]["status"] == "error"
    assert result["discovery"]["error"] == "simulated discovery error"
    assert result["discovered_models"] == []


async def test_list_models_page_is_one_indexed(
    gateway_service: GatewayService, fake_ctx: FakeContext
) -> None:
    """The first page is 1 and the default is page 1."""
    result = await gateway_service.list_models(ctx=fake_ctx)

    assert fake_ctx.response.status_code == 200
    assert result["pagination"]["endpoints"]["current_page"] == 1


@pytest.mark.parametrize("page", [0, -1])
async def test_list_models_rejects_page_below_one(gateway_service: GatewayService, page: int) -> None:
    """Pages below 1 answer 400 with a structured error and no pagination."""
    ctx = FakeContext()

    body = await gateway_service.list_models(page=page, ctx=ctx)

    assert ctx.response.status_code == 400, page
    assert "page must be >= 1" in body["detail"]
    assert body["error"] == body["detail"]
    assert "pagination" not in body
