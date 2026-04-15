import unittest

from gateway.app.domain.helpers import (
    as_bool,
    is_valid_image_ref,
    is_valid_k8s_name,
    is_valid_registry_server,
    normalize_registry_server,
    positive_int_from_env,
    slug_to_model_name,
    to_model_slug,
)


class HelpersTest(unittest.TestCase):
    def test_as_bool(self) -> None:
        self.assertTrue(as_bool("true", default=False))
        self.assertTrue(as_bool("1", default=False))
        self.assertFalse(as_bool("false", default=True))
        self.assertFalse(as_bool("0", default=True))
        self.assertTrue(as_bool("unknown", default=True))

    def test_positive_int_from_env(self) -> None:
        env = {"A": "10", "B": "-5", "C": "abc"}
        self.assertEqual(positive_int_from_env(env, "A", 1), 10)
        self.assertEqual(positive_int_from_env(env, "B", 1), 1)
        self.assertEqual(positive_int_from_env(env, "C", 1), 1)
        self.assertEqual(positive_int_from_env(env, "MISSING", 7), 7)

    def test_normalize_registry_server(self) -> None:
        self.assertEqual(
            normalize_registry_server("https://registry.example.com/api-models"),
            "registry.example.com",
        )
        self.assertEqual(
            normalize_registry_server("registry.example.com"),
            "registry.example.com",
        )

    def test_validators_and_slug(self) -> None:
        self.assertTrue(is_valid_registry_server("registry.example.com"))
        self.assertFalse(is_valid_registry_server("https://registry.example.com"))

        self.assertTrue(is_valid_k8s_name("model-tiny-demo-model"))
        self.assertFalse(is_valid_k8s_name("Model_Upper"))

        self.assertTrue(is_valid_image_ref("registry.example.com/api-models/model:tag"))
        self.assertFalse(is_valid_image_ref("Docker.Invalid/Model"))

        self.assertEqual(to_model_slug("Tiny_Demo_Model"), "tiny-demo-model")
        self.assertEqual(slug_to_model_name("tiny-demo-model"), "tiny_demo_model")


if __name__ == "__main__":
    unittest.main()
