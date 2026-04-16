import unittest
from types import SimpleNamespace
from unittest.mock import patch

import model_service.app.domain.model_loader as model_loader


class ResolveModelUriTest(unittest.TestCase):
    def test_explicit_model_uri_has_priority(self) -> None:
        uri, version = model_loader.resolve_model_uri(
            model_name="demo",
            model_version="latest",
            model_uri="runs:/abc/model",
        )
        self.assertEqual(uri, "runs:/abc/model")
        self.assertEqual(version, "latest")

    def test_non_latest_version_returns_registry_uri(self) -> None:
        uri, version = model_loader.resolve_model_uri(
            model_name="demo",
            model_version="5",
            model_uri="",
        )
        self.assertEqual(uri, "models:/demo/5")
        self.assertEqual(version, "5")

    def test_latest_prefers_production(self) -> None:
        with patch.object(model_loader, "MlflowClient") as client_cls:
            client = client_cls.return_value
            client.get_registered_model.return_value = SimpleNamespace(
                latest_versions=[
                    SimpleNamespace(version="3", current_stage="Staging"),
                    SimpleNamespace(version="2", current_stage="Production"),
                    SimpleNamespace(version="8", current_stage="Production"),
                ]
            )

            uri, version = model_loader.resolve_model_uri(
                model_name="demo",
                model_version="latest",
                model_uri="",
            )

        self.assertEqual(uri, "models:/demo/8")
        self.assertEqual(version, "8")

    def test_latest_uses_highest_registered_when_no_production(self) -> None:
        with patch.object(model_loader, "MlflowClient") as client_cls:
            client = client_cls.return_value
            client.get_registered_model.return_value = SimpleNamespace(
                latest_versions=[
                    SimpleNamespace(version="1", current_stage="Staging"),
                    SimpleNamespace(version="4", current_stage="Archived"),
                    SimpleNamespace(version="3", current_stage=None),
                ]
            )

            uri, version = model_loader.resolve_model_uri(
                model_name="demo",
                model_version="latest",
                model_uri="",
            )

        self.assertEqual(uri, "models:/demo/4")
        self.assertEqual(version, "4")

    def test_latest_without_versions_raises(self) -> None:
        with patch.object(model_loader, "MlflowClient") as client_cls:
            client = client_cls.return_value
            client.get_registered_model.return_value = SimpleNamespace(latest_versions=[])

            with self.assertRaises(ValueError):
                model_loader.resolve_model_uri(
                    model_name="demo",
                    model_version="latest",
                    model_uri="",
                )


class LoadModelTest(unittest.TestCase):
    def test_load_model_wires_mlflow_calls(self) -> None:
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

        resolve_uri.assert_called_once_with(
            model_name="demo",
            model_version="latest",
            model_uri="",
        )
        set_tracking_uri.assert_called_once_with("http://mlflow:5000")
        load_mlflow_model.assert_called_once_with("models:/demo/7")
        self.assertEqual(model, "MODEL_OBJECT")
        self.assertEqual(resolved_uri, "models:/demo/7")
        self.assertEqual(effective_version, "7")


if __name__ == "__main__":
    unittest.main()

