"""Prediction handler for the model_service runtime."""

from typing import Any

import numpy as np
import pandas as pd


def input_column_names(model: Any) -> list[str]:
    """Named input columns of a tabular MLflow model, or [] for tensor/unknown models.

    Uses the MLflow signature when present and, for Ludwig models (logged without a
    signature), the input features declared in the `ludwig` flavor schema.
    """

    metadata = getattr(model, "metadata", None)
    if metadata is None:
        return []

    try:
        schema = metadata.get_input_schema()
    except Exception:
        schema = None
    if schema is not None and getattr(schema, "has_input_names", lambda: False)():
        names = schema.input_names()
        if names and all(isinstance(name, str) for name in names):
            # Tensor signatures with one named input are not tabular columns.
            if not getattr(schema, "is_tensor_spec", lambda: False)():
                return list(names)

    flavors = getattr(metadata, "flavors", None) or {}
    ludwig_schema = (flavors.get("ludwig") or {}).get("ludwig_schema") or {}
    columns = [
        feature.get("column") or feature.get("name")
        for feature in ludwig_schema.get("input_features") or []
    ]
    return [column for column in columns if column]


class PredictHandler:
    """Normalize request payloads and execute model predictions."""

    def __init__(self, model_name: str):
        self.model_name = model_name

    def _error(self, message: str) -> dict[str, Any]:
        return {"error": message, "model": self.model_name}

    def _features_as_columns(self, features: Any, columns: list[str]) -> pd.DataFrame | dict[str, Any]:
        """Map positional features onto the model's named input columns."""

        rows = features if features and isinstance(features[0], (list, tuple)) else [features]
        if any(isinstance(value, (list, tuple)) for row in rows for value in row):
            return self._error(
                "Invalid 'features' shape for a tabular model: use one row [v1, v2, ...] "
                "or a list of rows."
            )
        wrong = [len(row) for row in rows if len(row) != len(columns)]
        if wrong:
            return self._error(
                f"This model expects {len(columns)} features in this order: {columns} "
                f"(got {wrong[0]}). You can also send them by name: "
                + "{" + ", ".join(f'"{column}": ...' for column in columns) + "}."
            )
        return pd.DataFrame(rows, columns=columns)

    def predict(self, model: Any, data: dict[str, Any]) -> dict[str, Any]:
        """Run prediction and return a gateway-compatible response payload."""

        if model is None:
            return {"error": "Model not available"}

        columns: list[str] = []
        try:
            if "features" in data:
                features = data["features"]
                columns = input_column_names(model)
                if columns and isinstance(features, (list, tuple)):
                    # Tabular model with named inputs (e.g. Ludwig): positional values
                    # follow the column order of the model schema.
                    model_input = self._features_as_columns(list(features), columns)
                    if isinstance(model_input, dict):
                        return model_input
                    predictions = model.predict(model_input)
                else:
                    model_input = np.asarray(features, dtype=np.float32)

                    # Common gateway contract for tensor models:
                    # - 1D list -> one tabular sample: (1, n_features)
                    # - 2D list -> one temporal sample: (1, timesteps, n_features)
                    # - 3D list -> already batch format
                    if model_input.ndim == 1:
                        model_input = model_input.reshape(1, -1)
                    elif model_input.ndim == 2:
                        model_input = model_input.reshape(
                            1,
                            model_input.shape[0],
                            model_input.shape[1],
                        )
                    elif model_input.ndim != 3:
                        return self._error(
                            "Invalid 'features' shape. Use 1D, 2D or 3D numeric arrays."
                        )

                    predictions = model.predict(model_input)
            else:
                predictions = model.predict(pd.DataFrame([data]))

            if hasattr(predictions, "to_dict"):
                # Handle Pandas DataFrame outputs
                predictions = predictions.to_dict(orient="records")
            elif hasattr(predictions, "tolist"):
                # Handle Numpy array outputs
                predictions = predictions.tolist()
            elif not isinstance(predictions, list):
                # Handle basic iterables
                predictions = list(predictions)

            return {"model": self.model_name, "predictions": predictions}
        except Exception as exc:
            message = str(exc)
            if "features" in data and not columns:
                message += (
                    " (hint: if this model takes named columns, send them as "
                    '{"column_a": value, "column_b": value, ...} instead of "features")'
                )
            return self._error(message)
