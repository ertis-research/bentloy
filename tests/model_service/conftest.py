"""Shared fixtures for the model_service tests."""

import pytest

from model_service.app.config.settings import ModelServiceSettings
from model_service.app.handlers.predict_handler import PredictHandler


@pytest.fixture
def predict_handler() -> PredictHandler:
    """Return a PredictHandler for the model named ``demo``."""
    return PredictHandler(model_name="demo")


@pytest.fixture
def settings() -> ModelServiceSettings:
    """Return settings for the model ``tiny_demo_model`` with a placeholder MLflow URI."""
    return ModelServiceSettings(
        mlflow_tracking_uri="http://mlflow:5000",
        model_name="tiny_demo_model",
        model_version="latest",
        model_uri="",
    )
