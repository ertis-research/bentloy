"""Tests for HealthHandler."""

from model_service.app.handlers.health_handler import HealthHandler


def test_health_when_model_loaded() -> None:
    """A loaded model is reported as healthy with its metadata."""
    handler = HealthHandler(model_name="demo")
    payload = handler.health(model=object(), model_version="7", model_uri="models:/demo/7")

    assert payload["status"] == "healthy"
    assert payload["model"] == "demo"
    assert payload["model_loaded"] is True
    assert payload["model_version"] == "7"
    assert payload["model_uri"] == "models:/demo/7"


def test_health_when_model_missing() -> None:
    """A missing model is reported as unhealthy."""
    handler = HealthHandler(model_name="demo")
    payload = handler.health(model=None, model_version="latest", model_uri="")

    assert payload["status"] == "unhealthy"
    assert payload["model_loaded"] is False
    assert payload["model"] == "demo"
