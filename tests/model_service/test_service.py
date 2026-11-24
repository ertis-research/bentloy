"""Tests for the MLflowModelService BentoML runtime."""

import logging
from collections.abc import Iterator
from unittest.mock import MagicMock, patch

import pytest

import model_service.service as service_module
from model_service.app.config.settings import ModelServiceSettings

LOADED_MODEL = ("MODEL_OBJECT", "models:/tiny_demo_model/3", "3")


@pytest.fixture
def patched_env(settings: ModelServiceSettings) -> Iterator[None]:
    """Patch the settings loader so the service reads the ``settings`` fixture."""
    with patch.object(service_module.ModelServiceSettings, "from_env", return_value=settings):
        yield


@pytest.fixture
def service(patched_env: None) -> service_module.MLflowModelService:
    """Build a service whose model loads successfully."""
    with patch.object(service_module, "load_model", return_value=LOADED_MODEL):
        return service_module.MLflowModelService()


def test_init_requires_model_name(caplog: pytest.LogCaptureFixture) -> None:
    """A missing model name makes the service constructor fail."""
    settings = ModelServiceSettings(
        mlflow_tracking_uri="http://mlflow:5000",
        model_name="",
        model_version="latest",
        model_uri="",
    )
    with patch.object(service_module.ModelServiceSettings, "from_env", return_value=settings):
        # BentoML logs the traceback of a failing __init__; capture it to keep output clean.
        with caplog.at_level(logging.ERROR), pytest.raises(ValueError):
            service_module.MLflowModelService()

    assert caplog.records


def test_init_loads_model_and_sets_runtime_state(patched_env: None) -> None:
    """The constructor loads the model and stores its version and URI."""
    with patch.object(service_module, "load_model", return_value=LOADED_MODEL) as load_model:
        service = service_module.MLflowModelService()

    load_model.assert_called_once_with(
        mlflow_tracking_uri="http://mlflow:5000",
        model_name="tiny_demo_model",
        model_version="latest",
        model_uri="",
    )
    assert service.model == "MODEL_OBJECT"
    assert service.model_version == "3"
    assert service.model_uri == "models:/tiny_demo_model/3"


def test_predict_delegates_to_handler(service: service_module.MLflowModelService) -> None:
    """predict forwards the loaded model and payload to the handler."""
    service.predict_handler.predict = MagicMock(
        return_value={"model": "tiny_demo_model", "predictions": [1.0]}
    )
    payload = {"features": [1.0, 2.0, 3.0]}
    response = service.predict(payload)

    service.predict_handler.predict.assert_called_once_with(model="MODEL_OBJECT", data=payload)
    assert response["predictions"] == [1.0]


def test_health_delegates_to_handler(service: service_module.MLflowModelService) -> None:
    """health forwards the model state to the handler."""
    service.health_handler.health = MagicMock(
        return_value={
            "status": "healthy",
            "model": "tiny_demo_model",
            "model_loaded": True,
            "model_version": "3",
            "model_uri": "models:/tiny_demo_model/3",
        }
    )
    response = service.health()

    service.health_handler.health.assert_called_once_with(
        model="MODEL_OBJECT",
        model_version="3",
        model_uri="models:/tiny_demo_model/3",
    )
    assert response["status"] == "healthy"


def test_init_handles_load_model_error_and_keeps_service_alive(patched_env: None) -> None:
    """A model load failure leaves the service alive without a model."""
    with patch.object(service_module, "load_model", side_effect=RuntimeError("load failed")):
        service = service_module.MLflowModelService()

    assert service.model is None
    assert service.model_name == "tiny_demo_model"
