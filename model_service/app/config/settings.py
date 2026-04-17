"""Environment-based configuration loading for model_service."""

import os
from dataclasses import dataclass
from typing import Mapping


@dataclass(frozen=True)
class ModelServiceSettings:
    """Runtime settings required to load and serve an MLflow model."""

    mlflow_tracking_uri: str
    model_name: str
    model_version: str
    model_uri: str

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> "ModelServiceSettings":
        """Build settings from environment variables with sensible defaults."""

        source = env if env is not None else os.environ

        model_name = source.get("MODEL_NAME", "").strip()
        model_version = source.get("MODEL_VERSION", "latest").strip() or "latest"
        model_uri = source.get("MODEL_URI", "").strip()

        return cls(
            mlflow_tracking_uri=source.get(
                "MLFLOW_TRACKING_URI",
                "http://localhost:5000",
            ).strip(),
            model_name=model_name,
            model_version=model_version,
            model_uri=model_uri,
        )

