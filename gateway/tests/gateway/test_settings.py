import unittest

from gateway.app.config.settings import GatewaySettings


class SettingsTest(unittest.TestCase):
    def test_defaults(self) -> None:
        settings = GatewaySettings.from_env({})
        self.assertEqual(settings.model_service_prefix, "model")
        self.assertEqual(settings.model_namespace, "default")
        self.assertEqual(settings.model_url_template, "http://{service_name}:3000")
        self.assertTrue(settings.auto_deploy_enabled)
        self.assertEqual(settings.registry_server, "")
        self.assertEqual(settings.registry_secret, "model-registry-creds")
        self.assertEqual(settings.build_timeout_sec, 1800)
        self.assertEqual(settings.rollout_timeout_sec, 300)
        self.assertEqual(settings.model_urls, {})

    def test_custom_values_and_model_urls(self) -> None:
        env = {
            "MODEL_SERVICE_PREFIX": "ms",
            "MODEL_NAMESPACE": "api-models",
            "MODEL_URL_TEMPLATE": "http://{service_name}.{namespace}:3000",
            "AUTO_DEPLOY_ENABLED": "false",
            "REGISTRY_SERVER": "https://registry.example.com/projects",
            "TU_ORG": "api-models",
            "MODEL_BASE_IMAGE": "registry.example.com/api-models/model-service-base:py310",
            "MODEL_REGISTRY_SECRET": "model-registry-creds",
            "KANIKO_IMAGE": "gcr.io/kaniko-project/executor:v1.23.2-debug",
            "KANIKO_SKIP_TLS_VERIFY": "1",
            "MLFLOW_TRACKING_URI": "http://mlflow.example.com:5000",
            "DEPLOY_BUILD_TIMEOUT_SEC": "2400",
            "DEPLOY_ROLLOUT_TIMEOUT_SEC": "450",
            "MODELO_TINY_DEMO_MODEL_URL": "http://model-tiny-demo-model:3000",
        }

        settings = GatewaySettings.from_env(env)
        self.assertEqual(settings.model_service_prefix, "ms")
        self.assertEqual(settings.model_namespace, "api-models")
        self.assertEqual(
            settings.model_url_template,
            "http://{service_name}.{namespace}:3000",
        )
        self.assertFalse(settings.auto_deploy_enabled)
        self.assertEqual(settings.registry_server, "registry.example.com")
        self.assertEqual(settings.registry_org, "api-models")
        self.assertTrue(settings.kaniko_skip_tls_verify_default)
        self.assertEqual(settings.default_tracking_uri, "http://mlflow.example.com:5000")
        self.assertEqual(settings.build_timeout_sec, 2400)
        self.assertEqual(settings.rollout_timeout_sec, 450)
        self.assertEqual(
            settings.model_urls,
            {"tiny_demo_model": "http://model-tiny-demo-model:3000"},
        )


if __name__ == "__main__":
    unittest.main()
