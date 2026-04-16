import unittest

from model_service.app.handlers.health_handler import HealthHandler


class HealthHandlerTest(unittest.TestCase):
    def test_health_when_model_loaded(self) -> None:
        handler = HealthHandler(model_name="demo")
        payload = handler.health(model=object(), model_version="7", model_uri="models:/demo/7")

        self.assertEqual(payload["status"], "healthy")
        self.assertEqual(payload["model"], "demo")
        self.assertTrue(payload["model_loaded"])
        self.assertEqual(payload["model_version"], "7")
        self.assertEqual(payload["model_uri"], "models:/demo/7")

    def test_health_when_model_missing(self) -> None:
        handler = HealthHandler(model_name="demo")
        payload = handler.health(model=None, model_version="latest", model_uri="")

        self.assertEqual(payload["status"], "unhealthy")
        self.assertFalse(payload["model_loaded"])
        self.assertEqual(payload["model"], "demo")


if __name__ == "__main__":
    unittest.main()

