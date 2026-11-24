"""Tests for model URI resolution and MLflow model loading."""

from types import SimpleNamespace
from unittest.mock import patch

import pytest

import model_service.app.domain.model_loader as model_loader


def _versions(*pairs: tuple[str, str | None]) -> SimpleNamespace:
    """Build a registered-model double from (version, stage) pairs."""
    return SimpleNamespace(
        latest_versions=[SimpleNamespace(version=v, current_stage=s) for v, s in pairs]
    )


def test_explicit_model_uri_has_priority() -> None:
    """An explicit model URI wins over name and version."""
    uri, version = model_loader.resolve_model_uri(
        model_name="demo", model_version="latest", model_uri="runs:/abc/model"
    )
    assert uri == "runs:/abc/model"
    assert version == "latest"


def test_non_latest_version_returns_registry_uri() -> None:
    """A numeric version maps directly to a registry URI."""
    uri, version = model_loader.resolve_model_uri(
        model_name="demo", model_version="5", model_uri=""
    )
    assert uri == "models:/demo/5"
    assert version == "5"


@pytest.mark.parametrize(
    ("registered", "expected_version"),
    [
        pytest.param(
            _versions(("3", "Staging"), ("2", "Production"), ("8", "Production")),
            "8",
            id="prefers-production",
        ),
        pytest.param(
            _versions(("1", "Staging"), ("4", "Archived"), ("3", None)),
            "4",
            id="highest-when-no-production",
        ),
    ],
)
def test_latest_resolves_registered_version(registered: SimpleNamespace, expected_version: str) -> None:
    """``latest`` resolves to the highest Production version, else the highest version."""
    with patch.object(model_loader, "MlflowClient") as client_cls:
        client_cls.return_value.get_registered_model.return_value = registered

        uri, version = model_loader.resolve_model_uri(
            model_name="demo", model_version="latest", model_uri=""
        )

    assert uri == f"models:/demo/{expected_version}"
    assert version == expected_version


def test_latest_without_versions_raises() -> None:
    """A registered model without versions cannot be resolved."""
    with patch.object(model_loader, "MlflowClient") as client_cls:
        client_cls.return_value.get_registered_model.return_value = SimpleNamespace(
            latest_versions=[]
        )

        with pytest.raises(ValueError):
            model_loader.resolve_model_uri(
                model_name="demo", model_version="latest", model_uri=""
            )


def test_load_model_wires_mlflow_calls() -> None:
    """load_model resolves the URI, sets the tracking URI and loads the pyfunc model."""
    with (
        patch.object(model_loader, "resolve_model_uri", return_value=("models:/demo/7", "7")) as resolve_uri,
        patch("model_service.app.domain.model_loader.mlflow.set_tracking_uri") as set_tracking_uri,
        patch(
            "model_service.app.domain.model_loader.mlflow.pyfunc.load_model",
            return_value="MODEL_OBJECT",
        ) as load_mlflow_model,
    ):
        model, resolved_uri, effective_version = model_loader.load_model(
            mlflow_tracking_uri="http://mlflow:5000",
            model_name="demo",
            model_version="latest",
            model_uri="",
        )

    resolve_uri.assert_called_once_with(model_name="demo", model_version="latest", model_uri="")
    set_tracking_uri.assert_called_once_with("http://mlflow:5000")
    load_mlflow_model.assert_called_once_with("models:/demo/7")
    assert model == "MODEL_OBJECT"
    assert resolved_uri == "models:/demo/7"
    assert effective_version == "7"
