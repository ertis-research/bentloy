"""MLflow model URI resolution and model-loading helpers."""

from typing import Any

import mlflow
import mlflow.pyfunc
from mlflow.tracking.client import MlflowClient


def resolve_model_uri(
    model_name: str,
    model_version: str,
    model_uri: str,
) -> tuple[str, str]:
    """Resolve a concrete MLflow URI and the effective model version."""
    if model_uri:
        return model_uri, model_version

    if model_version != "latest":
        return f"models:/{model_name}/{model_version}", model_version

    latest_versions = MlflowClient().get_registered_model(model_name).latest_versions
    if not latest_versions:
        raise ValueError(f"No registered versions found for MODEL_NAME={model_name}")

    production_versions = [
        version.version
        for version in latest_versions
        if (version.current_stage or "").lower() == "production"
    ]
    if production_versions:
        chosen_version = max(production_versions, key=lambda value: int(value))
        print(f"[{model_name}] latest resolved using Production stage -> v{chosen_version}")
        return f"models:/{model_name}/{chosen_version}", chosen_version

    available_versions = [version.version for version in latest_versions if version.version]
    if not available_versions:
        raise ValueError(f"No valid versions found for MODEL_NAME={model_name}")

    chosen_version = max(available_versions, key=lambda value: int(value))
    print(f"[{model_name}] latest resolved to highest registered version -> v{chosen_version}")
    return f"models:/{model_name}/{chosen_version}", chosen_version


def load_model(
    mlflow_tracking_uri: str,
    model_name: str,
    model_version: str,
    model_uri: str,
) -> tuple[Any, str, str]:
    """Load model and return (model, resolved_uri, effective_version)."""
    mlflow.set_tracking_uri(mlflow_tracking_uri)

    resolved_uri, effective_version = resolve_model_uri(
        model_name=model_name,
        model_version=model_version,
        model_uri=model_uri,
    )

    print(f"[{model_name}] Loading model from: {resolved_uri}")
    model = mlflow.pyfunc.load_model(resolved_uri)
    print(f"[{model_name}] Model loaded")
    return model, resolved_uri, effective_version

