from typing import Any


class HealthHandler:
    def __init__(self, model_name: str):
        self.model_name = model_name

    def health(
        self,
        model: Any,
        model_version: str,
        model_uri: str,
    ) -> dict[str, Any]:
        return {
            "status": "healthy" if model else "unhealthy",
            "model": self.model_name,
            "model_loaded": model is not None,
            "model_version": model_version,
            "model_uri": model_uri,
        }

