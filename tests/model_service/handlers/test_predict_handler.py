"""Tests for PredictHandler, including tabular (named-column) models."""

import numpy as np
from mlflow.types.schema import ColSpec, Schema, TensorSpec

from model_service.app.handlers.predict_handler import PredictHandler
from tests.model_service.fakes import (
    LUDWIG_FLAVORS,
    CapturingModel,
    FailingModel,
    Metadata,
    TabularModel,
)


def test_predict_returns_error_when_model_is_none(predict_handler: PredictHandler) -> None:
    """Predicting without a loaded model returns an error payload."""
    result = predict_handler.predict(model=None, data={"features": [1.0, 2.0]})
    assert result == {"error": "Model not available"}


def test_features_1d_are_reshaped_to_single_sample(predict_handler: PredictHandler) -> None:
    """A flat feature list becomes a (1, n) array."""
    model = CapturingModel(output=np.array([0.7]))

    result = predict_handler.predict(model=model, data={"features": [1.0, 2.0, 3.0]})

    assert tuple(model.last_input.shape) == (1, 3)
    assert result["model"] == "demo"
    assert result["predictions"] == [0.7]


def test_features_2d_are_reshaped_to_temporal_single_sample(predict_handler: PredictHandler) -> None:
    """A 2-D feature list becomes a (1, t, n) array."""
    model = CapturingModel(output=np.array([[1.0]]))

    result = predict_handler.predict(model=model, data={"features": [[1.0, 2.0], [3.0, 4.0]]})

    assert tuple(model.last_input.shape) == (1, 2, 2)
    assert result["predictions"] == [[1.0]]


def test_invalid_feature_ndim_returns_validation_error(predict_handler: PredictHandler) -> None:
    """Features with too many dimensions return a validation error."""
    model = CapturingModel(output=np.array([1.0]))

    result = predict_handler.predict(model=model, data={"features": [[[[1.0]]]]})

    assert "Invalid 'features' shape" in result["error"]
    assert result["model"] == "demo"


def test_dict_payload_uses_dataframe_path(predict_handler: PredictHandler) -> None:
    """A dict of named values is passed to the model as a DataFrame."""
    model = CapturingModel(output=np.array([2.5]))

    result = predict_handler.predict(model=model, data={"a": 1.0, "b": 2.0})

    assert list(model.last_input.columns) == ["a", "b"]
    assert result["predictions"] == [2.5]


def test_model_exception_is_returned_as_error(predict_handler: PredictHandler) -> None:
    """Model failures are returned as error payloads, with a hint for ``features`` input."""
    result = predict_handler.predict(model=FailingModel(), data={"features": [1.0]})

    assert result["error"].startswith("boom")
    assert "named columns" in result["error"]
    assert result["model"] == "demo"

    result = predict_handler.predict(model=FailingModel(), data={"a": 1.0})
    assert result["error"] == "boom"


def test_response_has_no_extra_fields(predict_handler: PredictHandler) -> None:
    """A successful response only contains ``model`` and ``predictions``."""
    result = predict_handler.predict(model=CapturingModel(output=np.array([1.0])), data={"a": 1.0})
    assert set(result) == {"model", "predictions"}


def test_ludwig_features_become_named_columns(predict_handler: PredictHandler) -> None:
    """Ludwig input features are mapped to named columns."""
    model = TabularModel(np.array([4.8]), Metadata(flavors=LUDWIG_FLAVORS))
    result = predict_handler.predict(model=model, data={"features": [1, 2, 3]})

    assert list(model.last_input.columns) == ["x1", "x2", "x3"]
    assert model.last_input.iloc[0].tolist() == [1, 2, 3]
    assert result["predictions"] == [4.8]


def test_ludwig_features_accept_several_rows(predict_handler: PredictHandler) -> None:
    """Several feature rows produce a multi-row DataFrame."""
    model = TabularModel(np.array([1.0, 2.0]), Metadata(flavors=LUDWIG_FLAVORS))
    predict_handler.predict(model=model, data={"features": [[1, 2, 3], [4, 5, 6]]})
    assert model.last_input.shape == (2, 3)


def test_wrong_feature_count_explains_expected_columns(predict_handler: PredictHandler) -> None:
    """A wrong feature count lists the expected columns and skips the model."""
    model = TabularModel(np.array([1.0]), Metadata(flavors=LUDWIG_FLAVORS))
    result = predict_handler.predict(model=model, data={"features": [1, 2]})

    assert "expects 3 features" in result["error"]
    assert "['x1', 'x2', 'x3']" in result["error"]
    assert model.last_input is None


def test_nested_features_are_rejected_for_tabular_models(predict_handler: PredictHandler) -> None:
    """Three-level nesting is invalid for tabular models."""
    model = TabularModel(np.array([1.0]), Metadata(flavors=LUDWIG_FLAVORS))
    result = predict_handler.predict(model=model, data={"features": [[[1, 2, 3]]]})
    assert "Invalid 'features' shape for a tabular model" in result["error"]


def test_mlflow_column_signature_is_used(predict_handler: PredictHandler) -> None:
    """An MLflow column signature names the DataFrame columns."""
    schema = Schema([ColSpec("double", "a"), ColSpec("double", "b")])
    model = TabularModel(np.array([0.5]), Metadata(schema=schema))
    predict_handler.predict(model=model, data={"features": [1.0, 2.0]})
    assert list(model.last_input.columns) == ["a", "b"]


def test_tensor_signature_keeps_array_path(predict_handler: PredictHandler) -> None:
    """A tensor signature keeps the plain array input."""
    schema = Schema([TensorSpec(np.dtype(np.float32), (-1, 3))])
    model = TabularModel(np.array([0.5]), Metadata(schema=schema))
    predict_handler.predict(model=model, data={"features": [1.0, 2.0, 3.0]})
    assert tuple(model.last_input.shape) == (1, 3)
