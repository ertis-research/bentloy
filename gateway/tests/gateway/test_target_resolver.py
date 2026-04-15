import unittest

from gateway.app.routing.target_resolver import PredictTargetResolver


class PredictTargetResolverTest(unittest.TestCase):
    def setUp(self) -> None:
        self.resolver = PredictTargetResolver(
            model_urls={
                "tiny_demo_model": "http://mapped-model:3000",
            },
            model_service_prefix="model",
            model_namespace="api-models",
            model_url_template="http://{service_name}:3000",
        )

    def test_priority_explicit_model_url_overrides_mapping(self) -> None:
        predict_url, base_url, source = self.resolver.resolve_predict_target(
            model_name="tiny_demo_model",
            model_url="http://external-host:3000",
        )
        self.assertEqual(source, "external_url")
        self.assertEqual(base_url, "http://external-host:3000")
        self.assertEqual(predict_url, "http://external-host:3000/predict")

    def test_priority_env_mapping_over_k8s_convention(self) -> None:
        predict_url, base_url, source = self.resolver.resolve_predict_target(
            model_name="tiny_demo_model",
        )
        self.assertEqual(source, "env_mapping")
        self.assertEqual(base_url, "http://mapped-model:3000")
        self.assertEqual(predict_url, "http://mapped-model:3000/predict")

    def test_fallback_to_k8s_convention(self) -> None:
        predict_url, base_url, source = self.resolver.resolve_predict_target(
            model_name="another_model",
        )
        self.assertEqual(source, "k8s_convention")
        self.assertEqual(base_url, "http://model-another-model:3000")
        self.assertEqual(predict_url, "http://model-another-model:3000/predict")

    def test_external_url_with_predict_suffix(self) -> None:
        predict_url, base_url, source = self.resolver.resolve_predict_target(
            model_name="ignored",
            model_url="https://external-host/predict",
        )
        self.assertEqual(source, "external_url")
        self.assertEqual(base_url, "https://external-host")
        self.assertEqual(predict_url, "https://external-host/predict")

    def test_invalid_model_url_raises(self) -> None:
        with self.assertRaises(ValueError):
            self.resolver.resolve_predict_target(
                model_name="tiny_demo_model",
                model_url="ftp://invalid-url",
            )

        with self.assertRaises(ValueError):
            self.resolver.resolve_predict_target(
                model_name="tiny_demo_model",
                model_url="http://external-host/path?bad=query",
            )


if __name__ == "__main__":
    unittest.main()
