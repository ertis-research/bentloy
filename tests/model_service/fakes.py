"""Fake collaborators shared by the model_service tests."""

from typing import Any


class CapturingModel:
    """Model double that records the last input it received and returns a fixed output."""

    def __init__(self, output: Any) -> None:
        """Store the output returned by every call to ``predict``."""
        self.output = output
        self.last_input: Any = None

    def predict(self, model_input: Any) -> Any:
        """Record the input and return the configured output."""
        self.last_input = model_input
        return self.output


class FailingModel:
    """Model double whose ``predict`` always raises."""

    def predict(self, _: Any) -> Any:
        """Raise a RuntimeError with a fixed message."""
        raise RuntimeError("boom")


class Metadata:
    """Minimal stand-in for MLflow model metadata."""

    def __init__(self, schema: Any = None, flavors: dict[str, Any] | None = None) -> None:
        """Store the input schema and the flavors mapping."""
        self._schema = schema
        self.flavors = flavors or {}

    def get_input_schema(self) -> Any:
        """Return the configured input schema."""
        return self._schema


class TabularModel(CapturingModel):
    """Capturing model that also exposes MLflow-like metadata."""

    def __init__(self, output: Any, metadata: Metadata) -> None:
        """Store the output and the metadata object."""
        super().__init__(output)
        self.metadata = metadata


LUDWIG_FLAVORS: dict[str, Any] = {
    "ludwig": {
        "ludwig_schema": {
            "input_features": [
                {"column": "x1", "name": "x1", "type": "number"},
                {"column": "x2", "name": "x2", "type": "number"},
                {"column": "x3", "name": "x3", "type": "number"},
            ],
            "output_features": [{"column": "y", "name": "y", "type": "number"}],
        }
    },
    "python_function": {"loader_module": "ludwig.contribs.mlflow.model"},
}
