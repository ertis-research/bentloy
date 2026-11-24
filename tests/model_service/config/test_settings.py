"""Tests for ModelServiceSettings."""

from model_service.app.config.settings import ModelServiceSettings


def test_defaults() -> None:
    """An empty environment yields the documented defaults."""
    settings = ModelServiceSettings.from_env({})
    assert settings.mlflow_tracking_uri == "http://localhost:5000"
    assert settings.model_name == ""
    assert settings.model_version == "latest"
    assert settings.model_uri == ""


def test_custom_values() -> None:
    """Environment values are read and stripped."""
    settings = ModelServiceSettings.from_env(
        {
            "MLFLOW_TRACKING_URI": "http://mlflow:5000",
            "MODEL_NAME": " tiny_demo_model ",
            "MODEL_VERSION": " 7 ",
            "MODEL_URI": " runs:/abc123/model ",
        }
    )
    assert settings.mlflow_tracking_uri == "http://mlflow:5000"
    assert settings.model_name == "tiny_demo_model"
    assert settings.model_version == "7"
    assert settings.model_uri == "runs:/abc123/model"


def test_model_version_empty_falls_back_to_latest() -> None:
    """A blank MODEL_VERSION falls back to ``latest``."""
    settings = ModelServiceSettings.from_env({"MODEL_NAME": "demo", "MODEL_VERSION": "   "})
    assert settings.model_version == "latest"
