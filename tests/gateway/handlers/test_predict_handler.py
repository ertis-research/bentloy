"""Tests for the gateway PredictHandler."""

from collections.abc import Callable

import pytest

from gateway.app.handlers.predict_handler import PredictHandler
from tests.gateway.fakes import FakePredictClient, FakeResponse, FakeTargetResolver


@pytest.fixture
def resolver() -> FakeTargetResolver:
    """Return a resolver double that succeeds by default."""
    return FakeTargetResolver()


@pytest.fixture
def predict_client() -> FakePredictClient:
    """Return a predict client double that echoes requests by default."""
    return FakePredictClient()


@pytest.fixture
def make_handler(
    resolver: FakeTargetResolver, predict_client: FakePredictClient
) -> Callable[..., PredictHandler]:
    """Return a factory for PredictHandler instances using the resolver and client doubles."""

    def factory(
        model_urls: dict[str, str] | None = None,
        recently_routed_models: dict[str, str] | None = None,
    ) -> PredictHandler:
        """Build a PredictHandler with optional model URLs and recent-model registry."""
        return PredictHandler(
            target_resolver=resolver,  # type: ignore[arg-type]
            model_urls=model_urls or {},
            recently_routed_models=recently_routed_models if recently_routed_models is not None else {},
            client_factory=lambda timeout: predict_client,
        )

    return factory


async def test_predict_success_updates_recent_model(make_handler: Callable[..., PredictHandler]) -> None:
    """A successful prediction is returned and the model is recorded as recently routed."""
    recently_routed_models: dict[str, str] = {}
    handler = make_handler(
        model_urls={"tiny_demo_model": "http://model-tiny-demo-model:3000"},
        recently_routed_models=recently_routed_models,
    )

    result = await handler.predict(model_name="tiny_demo_model", data={"x": 1})

    assert result["ok"] is True
    assert "tiny_demo_model" in recently_routed_models
    assert recently_routed_models["tiny_demo_model"] == "http://model-tiny_demo_model:3000"


async def test_predict_returns_validation_error(
    make_handler: Callable[..., PredictHandler], resolver: FakeTargetResolver
) -> None:
    """An invalid model_url returns an error payload with a hint."""
    resolver.raise_value_error = True

    result = await make_handler().predict(model_name="tiny_demo_model", data={}, model_url="bad-url")

    assert result["error"] == "invalid model_url"
    assert result["model"] == "tiny_demo_model"
    assert "model_url" in result["hint"]


async def test_predict_returns_timeout_error(
    make_handler: Callable[..., PredictHandler], predict_client: FakePredictClient
) -> None:
    """A model timeout returns an error payload naming the model."""
    predict_client.raise_timeout = True

    result = await make_handler().predict(model_name="tiny_demo_model", data={})

    assert result == {"error": "Timeout: tiny_demo_model"}


async def test_batch_predict_mixed(
    make_handler: Callable[..., PredictHandler], predict_client: FakePredictClient
) -> None:
    """A batch returns one result per item, reporting items without model_name as errors."""
    predict_client.responses = [FakeResponse({"prediction": 0.1})]

    result = await make_handler().batch_predict(
        [
            {"model_name": "tiny_demo_model", "data": {"x": 1}},
            {"data": {"x": 2}},
        ]
    )

    assert result["results"][0] == {"prediction": 0.1}
    assert result["results"][1] == {"error": "model_name is required"}
