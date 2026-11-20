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

        self.assertTrue(result["error"].startswith("boom"))
        self.assertIn("named columns", result["error"])
        self.assertEqual(result["model"], "demo")

        result = handler.predict(model=_FailingModel(), data={"a": 1.0})
        self.assertEqual(result["error"], "boom")

    def test_response_has_no_extra_fields(self) -> None:
        handler = PredictHandler(model_name="demo")
        result = handler.predict(model=_CapturingModel(output=np.array([1.0])), data={"a": 1.0})
        self.assertEqual(set(result), {"model", "predictions"})


class _Metadata:
    def __init__(self, schema=None, flavors=None):
        self._schema = schema
        self.flavors = flavors or {}

    def get_input_schema(self):
        return self._schema


class _TabularModel(_CapturingModel):
    def __init__(self, output, metadata):
        super().__init__(output)
        self.metadata = metadata


LUDWIG_FLAVORS = {
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


class TabularFeaturesTest(unittest.TestCase):
    def test_ludwig_features_become_named_columns(self) -> None:
        model = _TabularModel(np.array([4.8]), _Metadata(flavors=LUDWIG_FLAVORS))
        result = PredictHandler("demo").predict(model=model, data={"features": [1, 2, 3]})

        self.assertEqual(list(model.last_input.columns), ["x1", "x2", "x3"])
        self.assertEqual(model.last_input.iloc[0].tolist(), [1, 2, 3])
        self.assertEqual(result["predictions"], [4.8])

    def test_ludwig_features_accept_several_rows(self) -> None:
        model = _TabularModel(np.array([1.0, 2.0]), _Metadata(flavors=LUDWIG_FLAVORS))
        PredictHandler("demo").predict(model=model, data={"features": [[1, 2, 3], [4, 5, 6]]})
        self.assertEqual(model.last_input.shape, (2, 3))

    def test_wrong_feature_count_explains_expected_columns(self) -> None:
        model = _TabularModel(np.array([1.0]), _Metadata(flavors=LUDWIG_FLAVORS))
        result = PredictHandler("demo").predict(model=model, data={"features": [1, 2]})

        self.assertIn("expects 3 features", result["error"])
        self.assertIn("['x1', 'x2', 'x3']", result["error"])
        self.assertIsNone(model.last_input)

    def test_nested_features_are_rejected_for_tabular_models(self) -> None:
        model = _TabularModel(np.array([1.0]), _Metadata(flavors=LUDWIG_FLAVORS))
        result = PredictHandler("demo").predict(model=model, data={"features": [[[1, 2, 3]]]})
        self.assertIn("Invalid 'features' shape for a tabular model", result["error"])

    def test_mlflow_column_signature_is_used(self) -> None:
        from mlflow.types.schema import ColSpec, Schema

        schema = Schema([ColSpec("double", "a"), ColSpec("double", "b")])
        model = _TabularModel(np.array([0.5]), _Metadata(schema=schema))
        PredictHandler("demo").predict(model=model, data={"features": [1.0, 2.0]})
        self.assertEqual(list(model.last_input.columns), ["a", "b"])

    def test_tensor_signature_keeps_array_path(self) -> None:
        from mlflow.types.schema import Schema, TensorSpec

        schema = Schema([TensorSpec(np.dtype(np.float32), (-1, 3))])
        model = _TabularModel(np.array([0.5]), _Metadata(schema=schema))
        PredictHandler("demo").predict(model=model, data={"features": [1.0, 2.0, 3.0]})
        self.assertEqual(tuple(model.last_input.shape), (1, 3))


if __name__ == "__main__":
    unittest.main()

