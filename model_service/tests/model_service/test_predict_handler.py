import unittest

import numpy as np

from model_service.app.handlers.predict_handler import PredictHandler


class _CapturingModel:
    def __init__(self, output):
        self.output = output
        self.last_input = None

    def predict(self, model_input):
        self.last_input = model_input
        return self.output


class PredictHandlerTest(unittest.TestCase):
    def test_predict_returns_error_when_model_is_none(self) -> None:
        handler = PredictHandler(model_name="demo")
        result = handler.predict(model=None, data={"features": [1.0, 2.0]})
        self.assertEqual(result, {"error": "Model not available"})

    def test_features_1d_are_reshaped_to_single_sample(self) -> None:
        handler = PredictHandler(model_name="demo")
        model = _CapturingModel(output=np.array([0.7]))

        result = handler.predict(model=model, data={"features": [1.0, 2.0, 3.0]})

        self.assertEqual(tuple(model.last_input.shape), (1, 3))
        self.assertEqual(result["model"], "demo")
        self.assertEqual(result["predictions"], [0.7])

    def test_features_2d_are_reshaped_to_temporal_single_sample(self) -> None:
        handler = PredictHandler(model_name="demo")
        model = _CapturingModel(output=np.array([[1.0]]))

        result = handler.predict(
            model=model,
            data={"features": [[1.0, 2.0], [3.0, 4.0]]},
        )

        self.assertEqual(tuple(model.last_input.shape), (1, 2, 2))
        self.assertEqual(result["predictions"], [[1.0]])

    def test_invalid_feature_ndim_returns_validation_error(self) -> None:
        handler = PredictHandler(model_name="demo")
        model = _CapturingModel(output=np.array([1.0]))

        result = handler.predict(
            model=model,
            data={"features": [[[[1.0]]]]},
        )

        self.assertIn("Invalid 'features' shape", result["error"])
        self.assertEqual(result["model"], "demo")

    def test_dict_payload_uses_dataframe_path(self) -> None:
        handler = PredictHandler(model_name="demo")
        model = _CapturingModel(output=np.array([2.5]))

        result = handler.predict(
            model=model,
            data={"a": 1.0, "b": 2.0},
        )

        self.assertEqual(list(model.last_input.columns), ["a", "b"])
        self.assertEqual(result["predictions"], [2.5])

    def test_model_exception_is_returned_as_error(self) -> None:
        class _FailingModel:
            def predict(self, _):
                raise RuntimeError("boom")

        handler = PredictHandler(model_name="demo")
        result = handler.predict(model=_FailingModel(), data={"features": [1.0]})

        self.assertEqual(result["error"], "boom")
        self.assertEqual(result["model"], "demo")


if __name__ == "__main__":
    unittest.main()

