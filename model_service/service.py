import os
from typing import Any

import bentoml
import mlflow
import mlflow.pyfunc
import numpy as np
import pandas as pd
from mlflow.tracking.client import MlflowClient


@bentoml.service(
    resources={"cpu": "2"},
    traffic={"timeout": 30},
)
class MLflowModelService:
    """
    Generic service for MLflow models.
    It is configured via environment variables.
    """

    def __init__(self):
        mlflow_uri = os.getenv("MLFLOW_TRACKING_URI", "http://localhost:5000")
        model_name = os.getenv("MODEL_NAME")
        model_version = os.getenv("MODEL_VERSION", "latest")
        model_uri = os.getenv("MODEL_URI")

        if not model_name:
            raise ValueError("MODEL_NAME is not configured")

        mlflow.set_tracking_uri(mlflow_uri)

        self.model = None
        self.model_name = model_name
        self.model_version = model_version
        self.model_uri = model_uri

        try:
            resolved_model_uri = self._resolve_model_uri(
                model_name=model_name,
                model_version=model_version,
                model_uri=model_uri,
            )

            print(f"[{model_name}] Loading model from: {resolved_model_uri}")
            self.model = mlflow.pyfunc.load_model(resolved_model_uri)
            self.model_uri = resolved_model_uri
            print(f"[{model_name}] Model loaded")

        except Exception as exc:
            print(f"[{model_name}] Error loading model: {exc}")

    def _resolve_model_uri(
        self,
        model_name: str,
        model_version: str,
        model_uri: str | None,
    ) -> str:
        # Explicit override (for example: runs:/<run_id>/model)
        if model_uri:
            return model_uri

        if model_version != "latest":
            return f"models:/{model_name}/{model_version}"

        # latest fallback strategy:
        # 1) pick latest in Production, if available
        # 2) otherwise pick highest registered numeric version
        latest_versions = MlflowClient().get_registered_model(model_name).latest_versions
        if not latest_versions:
            raise ValueError(f"No registered versions found for MODEL_NAME={model_name}")

        production_versions = [
            v.version
            for v in latest_versions
            if (v.current_stage or "").lower() == "production"
        ]
        if production_versions:
            chosen_version = max(production_versions, key=lambda value: int(value))
            self.model_version = chosen_version
            print(
                f"[{model_name}] latest resolved using Production stage -> v{chosen_version}"
            )
            return f"models:/{model_name}/{chosen_version}"

        available_versions = [v.version for v in latest_versions if v.version]
        if not available_versions:
            raise ValueError(f"No valid versions found for MODEL_NAME={model_name}")

        chosen_version = max(available_versions, key=lambda value: int(value))
        self.model_version = chosen_version
        print(
            f"[{model_name}] latest resolved to highest registered version -> v{chosen_version}"
        )
        return f"models:/{model_name}/{chosen_version}"

    @bentoml.api
    def predict(self, data: dict[str, Any]) -> dict[str, Any]:
        """
        Generic predict endpoint.

        Input:
          {"features": [1.0, 2.0, 3.0]}
          or {"col1": 1.0, "col2": 2.0}

        Output:
          {"model": "model_name", "predictions": [...]}
        """
        if self.model is None:
            return {"error": "Model not available"}

        try:
            if "features" in data:
                features = data["features"]
                model_input = np.asarray(features, dtype=np.float32)

                # Common gateway contract:
                # - 1D list -> one tabular sample: (1, n_features)
                # - 2D list -> one temporal sample: (1, timesteps, n_features)
                # - 3D list -> already batch format
                if model_input.ndim == 1:
                    model_input = model_input.reshape(1, -1)
                elif model_input.ndim == 2:
                    model_input = model_input.reshape(
                        1, model_input.shape[0], model_input.shape[1]
                    )
                elif model_input.ndim != 3:
                    return {
                        "error": (
                            "Invalid 'features' shape. "
                            "Use 1D, 2D or 3D numeric arrays."
                        ),
                        "model": self.model_name,
                    }

                predictions = self.model.predict(model_input)
            else:
                df = pd.DataFrame([data])
                predictions = self.model.predict(df)

            if hasattr(predictions, "tolist"):
                predictions = predictions.tolist()
            elif not isinstance(predictions, list):
                predictions = list(predictions)

            return {"model": self.model_name, "predictions": predictions}

        except Exception as exc:
            return {"error": str(exc), "model": self.model_name}

    @bentoml.api
    def health(self) -> dict[str, Any]:
        return {
            "status": "healthy" if self.model else "unhealthy",
            "model": self.model_name,
            "model_loaded": self.model is not None,
            "model_version": self.model_version,
            "model_uri": self.model_uri,
        }
