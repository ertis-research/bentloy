from typing import Any

import bentoml

try:
    # Package imports (tests/local tooling)
    from .app.config.settings import ModelServiceSettings
    from .app.domain.model_loader import load_model
    from .app.handlers.health_handler import HealthHandler
    from .app.handlers.predict_handler import PredictHandler
except ImportError:
    # Bento runtime imports when /app is working directory in container
    from app.config.settings import ModelServiceSettings
    from app.domain.model_loader import load_model
    from app.handlers.health_handler import HealthHandler
    from app.handlers.predict_handler import PredictHandler


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
        settings = ModelServiceSettings.from_env()

        if not settings.model_name:
            raise ValueError("MODEL_NAME is not configured")

        self.model = None
        self.model_name = settings.model_name
        self.model_version = settings.model_version
        self.model_uri = settings.model_uri
        self.predict_handler = PredictHandler(model_name=self.model_name)
        self.health_handler = HealthHandler(model_name=self.model_name)

        try:
            (
                self.model,
                self.model_uri,
                self.model_version,
            ) = load_model(
                mlflow_tracking_uri=settings.mlflow_tracking_uri,
                model_name=self.model_name,
                model_version=self.model_version,
                model_uri=self.model_uri,
            )
        except Exception as exc:
            print(f"[{self.model_name}] Error loading model: {exc}")

    @bentoml.api
    def predict(self, data: dict[str, Any]) -> dict[str, Any]:
        return self.predict_handler.predict(model=self.model, data=data)

    @bentoml.api
    def health(self) -> dict[str, Any]:
        return self.health_handler.health(
            model=self.model,
            model_version=self.model_version,
            model_uri=self.model_uri,
        )
