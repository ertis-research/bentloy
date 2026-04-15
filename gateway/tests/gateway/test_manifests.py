import unittest

from gateway.app.manifests.deployment_manifest import build_deployment_manifest
from gateway.app.manifests.job_manifest import build_job_manifest
from gateway.app.manifests.service_manifest import build_service_manifest


class ManifestBuildersTest(unittest.TestCase):
    def test_job_manifest_without_tls_flags(self) -> None:
        manifest = build_job_manifest(
            job_name="build-model-tiny-demo",
            namespace="api-models",
            model_name="tiny_demo_model",
            model_version="latest",
            model_uri="",
            mlflow_tracking_uri="http://mlflow:5000",
            target_image="registry.example.com/api-models/model-tiny-demo:latest",
            base_image="registry.example.com/api-models/model-service-base:py310",
            registry_secret="model-registry-creds",
            kaniko_image="gcr.io/kaniko-project/executor:v1.23.2-debug",
            insecure_registry_tls=False,
            registry_server="registry.example.com",
        )

        self.assertEqual(manifest["metadata"]["name"], "build-model-tiny-demo")
        self.assertEqual(manifest["metadata"]["namespace"], "api-models")

        pod_spec = manifest["spec"]["template"]["spec"]
        self.assertEqual(pod_spec["imagePullSecrets"], [{"name": "model-registry-creds"}])

        kaniko = next(c for c in pod_spec["containers"] if c["name"] == "kaniko")
        self.assertIn(
            "--destination=registry.example.com/api-models/model-tiny-demo:latest",
            kaniko["args"],
        )
        self.assertNotIn("--skip-tls-verify", kaniko["args"])

    def test_job_manifest_with_tls_flags(self) -> None:
        manifest = build_job_manifest(
            job_name="build-model-tiny-demo",
            namespace="api-models",
            model_name="tiny_demo_model",
            model_version="latest",
            model_uri="",
            mlflow_tracking_uri="http://mlflow:5000",
            target_image="registry.example.com/api-models/model-tiny-demo:latest",
            base_image="registry.example.com/api-models/model-service-base:py310",
            registry_secret="model-registry-creds",
            kaniko_image="gcr.io/kaniko-project/executor:v1.23.2-debug",
            insecure_registry_tls=True,
            registry_server="registry.example.com",
        )

        kaniko = next(
            c
            for c in manifest["spec"]["template"]["spec"]["containers"]
            if c["name"] == "kaniko"
        )
        self.assertIn("--skip-tls-verify", kaniko["args"])
        self.assertIn("--skip-tls-verify-pull", kaniko["args"])
        self.assertIn("--skip-tls-verify-registry=registry.example.com", kaniko["args"])

    def test_deployment_manifest(self) -> None:
        manifest = build_deployment_manifest(
            namespace="api-models",
            model_name="tiny_demo_model",
            model_slug="tiny-demo-model",
            model_image="registry.example.com/api-models/model-tiny-demo:latest",
            replicas=2,
            tracking_uri="http://mlflow:5000",
            model_uri="/models/model",
            model_version="image",
            registry_secret="model-registry-creds",
            model_service_prefix="model",
        )
        self.assertEqual(manifest["metadata"]["name"], "model-tiny-demo-model")
        self.assertEqual(manifest["metadata"]["namespace"], "api-models")
        self.assertEqual(manifest["spec"]["replicas"], 2)
        pod_spec = manifest["spec"]["template"]["spec"]
        self.assertEqual(pod_spec["imagePullSecrets"], [{"name": "model-registry-creds"}])

    def test_service_manifest(self) -> None:
        manifest = build_service_manifest(
            namespace="api-models",
            model_slug="tiny-demo-model",
            model_service_prefix="model",
        )
        self.assertEqual(manifest["metadata"]["name"], "model-tiny-demo-model")
        self.assertEqual(manifest["metadata"]["namespace"], "api-models")
        self.assertEqual(
            manifest["spec"]["selector"],
            {"app": "model-service", "model-slug": "tiny-demo-model"},
        )


if __name__ == "__main__":
    unittest.main()
