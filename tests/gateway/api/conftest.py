"""Fixtures for the GatewayService endpoint tests."""

from collections.abc import Awaitable, Callable

import pytest

from gateway.service import GatewayService
from tests.gateway.fakes import FakeContext

Deploy = Callable[..., Awaitable[tuple[int, dict]]]


@pytest.fixture
def deploy(gateway_service: GatewayService) -> Deploy:
    """Return a coroutine function that calls ``deploy_model`` and returns ``(status_code, body)``."""

    async def call(**kwargs) -> tuple[int, dict]:
        """Deploy ``tiny_demo_model`` with the given overrides and capture the HTTP status."""
        ctx = FakeContext()
        params = {
            "model_name": "tiny_demo_model",
            "tracking_uri": "http://mlflow:5000",
            "target_image": "registry.example.com/api-models/model-tiny-demo:latest",
            **kwargs,
        }
        result = await gateway_service.deploy_model(ctx=ctx, **params)
        return ctx.response.status_code, result

    return call
