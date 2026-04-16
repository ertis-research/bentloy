import unittest

from model_service.app.config.settings import ModelServiceSettings


class SettingsTest(unittest.TestCase):
    def test_defaults(self) -> None:
        settings = ModelServiceSettings.from_env({})
        self.assertEqual(settings.mlflow_tracking_uri, "http://localhost:5000")
        self.assertEqual(settings.model_name, "")
        self.assertEqual(settings.model_version, "latest")
        self.assertEqual(settings.model_uri, "")

    def test_custom_values(self) -> None:
        settings = ModelServiceSettings.from_env(
            {
                "MLFLOW_TRACKING_URI": "http://mlflow:5000",
                "MODEL_NAME": " tiny_demo_model ",
                "MODEL_VERSION": " 7 ",
                "MODEL_URI": " runs:/abc123/model ",
            }
        )
        self.assertEqual(settings.mlflow_tracking_uri, "http://mlflow:5000")
        self.assertEqual(settings.model_name, "tiny_demo_model")
        self.assertEqual(settings.model_version, "7")
        self.assertEqual(settings.model_uri, "runs:/abc123/model")

    def test_model_version_empty_falls_back_to_latest(self) -> None:
        settings = ModelServiceSettings.from_env(
            {"MODEL_NAME": "demo", "MODEL_VERSION": "   "}
        )
        self.assertEqual(settings.model_version, "latest")


if __name__ == "__main__":
    unittest.main()

