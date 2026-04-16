import unittest
from unittest.mock import MagicMock, patch

import model_service.service as service_module
from model_service.app.config.settings import ModelServiceSettings


class ModelServiceRuntimeTest(unittest.TestCase):
    def test_init_requires_model_name(self) -> None:
        settings = ModelServiceSettings(
            mlflow_tracking_uri="http://mlflow:5000",
            model_name="",
            model_version="latest",
            model_uri="",
        )
        with patch.object(
            service_module.ModelServiceSettings,
            "from_env",
            return_value=settings,
        ):
            with self.assertRaises(ValueError):
                service_module.MLflowModelService()

    def test_init_loads_model_and_sets_runtime_state(self) -> None:
        settings = ModelServiceSettings(
            mlflow_tracking_uri="http://mlflow:5000",
            model_name="tiny_demo_model",
            model_version="latest",
            model_uri="",
        )
        with (
            patch.object(
                service_module.ModelServiceSettings,
                "from_env",
                return_value=settings,
            ),
            patch.object(
                service_module,
                "load_model",
                return_value=("MODEL_OBJECT", "models:/tiny_demo_model/3", "3"),
            ) as load_model,
        ):
            service = service_module.MLflowModelService()

        load_model.assert_called_once_with(
            mlflow_tracking_uri="http://mlflow:5000",
            model_name="tiny_demo_model",
            model_version="latest",
            model_uri="",
        )
        self.assertEqual(service.model, "MODEL_OBJECT")
        self.assertEqual(service.model_version, "3")
        self.assertEqual(service.model_uri, "models:/tiny_demo_model/3")

    def test_predict_delegates_to_handler(self) -> None:
        settings = ModelServiceSettings(
            mlflow_tracking_uri="http://mlflow:5000",
            model_name="tiny_demo_model",
            model_version="latest",
            model_uri="",
        )
        with (
            patch.object(
                service_module.ModelServiceSettings,
                "from_env",
                return_value=settings,
            ),
            patch.object(
                service_module,
                "load_model",
                return_value=("MODEL_OBJECT", "models:/tiny_demo_model/3", "3"),
            ),
        ):
            service = service_module.MLflowModelService()

        service.predict_handler.predict = MagicMock(
            return_value={"model": "tiny_demo_model", "predictions": [1.0]}
        )
        payload = {"features": [1.0, 2.0, 3.0]}
        response = service.predict(payload)

        service.predict_handler.predict.assert_called_once_with(
            model="MODEL_OBJECT",
            data=payload,
        )
        self.assertEqual(response["predictions"], [1.0])

    def test_health_delegates_to_handler(self) -> None:
        settings = ModelServiceSettings(
            mlflow_tracking_uri="http://mlflow:5000",
            model_name="tiny_demo_model",
            model_version="latest",
            model_uri="",
        )
        with (
            patch.object(
                service_module.ModelServiceSettings,
                "from_env",
                return_value=settings,
            ),
            patch.object(
                service_module,
                "load_model",
                return_value=("MODEL_OBJECT", "models:/tiny_demo_model/3", "3"),
            ),
        ):
            service = service_module.MLflowModelService()

        service.health_handler.health = MagicMock(
            return_value={
                "status": "healthy",
                "model": "tiny_demo_model",
                "model_loaded": True,
                "model_version": "3",
                "model_uri": "models:/tiny_demo_model/3",
            }
        )
        response = service.health()

        service.health_handler.health.assert_called_once_with(
            model="MODEL_OBJECT",
            model_version="3",
            model_uri="models:/tiny_demo_model/3",
        )
        self.assertEqual(response["status"], "healthy")

    def test_init_handles_load_model_error_and_keeps_service_alive(self) -> None:
        settings = ModelServiceSettings(
            mlflow_tracking_uri="http://mlflow:5000",
            model_name="tiny_demo_model",
            model_version="latest",
            model_uri="",
        )
        with (
            patch.object(
                service_module.ModelServiceSettings,
                "from_env",
                return_value=settings,
            ),
            patch.object(service_module, "load_model", side_effect=RuntimeError("load failed")),
        ):
            service = service_module.MLflowModelService()

        self.assertIsNone(service.model)
        self.assertEqual(service.model_name, "tiny_demo_model")


if __name__ == "__main__":
    unittest.main()

