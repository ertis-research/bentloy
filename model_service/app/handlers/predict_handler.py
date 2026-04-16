from typing import Any

import numpy as np
import pandas as pd


class PredictHandler:
    def __init__(self, model_name: str):
        self.model_name = model_name

    def predict(self, model: Any, data: dict[str, Any]) -> dict[str, Any]:
        if model is None:
            return {"error": "Model not available"}

        try:
            if "features" in data:
                model_input = np.asarray(data["features"], dtype=np.float32)

                # Common gateway contract:
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
                    return {
                        "error": (
                            "Invalid 'features' shape. "
                            "Use 1D, 2D or 3D numeric arrays."
                        ),
                        "model": self.model_name,
                    }

                predictions = model.predict(model_input)
            else:
                predictions = model.predict(pd.DataFrame([data]))

            if hasattr(predictions, "tolist"):
                predictions = predictions.tolist()
            elif not isinstance(predictions, list):
                predictions = list(predictions)

            return {"model": self.model_name, "predictions": predictions}
        except Exception as exc:
            return {"error": str(exc), "model": self.model_name}

