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
        # Defaults: no cache (single snapshot) and no extra pip index.
        self.assertIn("--single-snapshot", kaniko["args"])
        self.assertFalse(any(arg.startswith("--cache") for arg in kaniko["args"]))
        self.assertFalse(any("MODEL_PIP_EXTRA_INDEX_URL" in arg for arg in kaniko["args"]))

    def test_job_manifest_with_cache_and_pip_index(self) -> None:
        manifest = build_job_manifest(
            job_name="build-model-tiny-demo",
            namespace="api-models",
            model_name="tiny_demo_model",
            model_version="latest",
            model_uri="",
            mlflow_tracking_uri="http://mlflow:5000",
            target_image="registry.example.com:5000/api-models/model-tiny-demo:v1",
            base_image="registry.example.com/api-models/model-service-base:py312",
            registry_secret="model-registry-creds",
            kaniko_image="gcr.io/kaniko-project/executor:v1.23.2-debug",
            insecure_registry_tls=False,
            registry_server="registry.example.com",
            pip_extra_index_url="https://download.pytorch.org/whl/cpu",
            kaniko_cache=True,
        )

        kaniko = next(
            c for c in manifest["spec"]["template"]["spec"]["containers"] if c["name"] == "kaniko"
        )
        self.assertIn("--cache=true", kaniko["args"])
        self.assertIn("--cache-repo=registry.example.com:5000/api-models/kaniko-cache", kaniko["args"])
        self.assertNotIn("--single-snapshot", kaniko["args"])
        self.assertIn(
            "--build-arg=MODEL_PIP_EXTRA_INDEX_URL=https://download.pytorch.org/whl/cpu",
            kaniko["args"],
        )

    def test_build_context_installs_dependencies_before_copying_the_model(self) -> None:
        from gateway.app.manifests.template_loader import load_template_text

        script = load_template_text("prepare_build_context.sh")
        dockerfile = script[script.index("ARG BASE_IMAGE"):script.index('"""\n)')]
        run_pip = dockerfile.index("RUN if [ -s /tmp/extra_requirements.txt ]")
        self.assertLess(run_pip, dockerfile.index("COPY model /models/model"))
        self.assertIn("ARG MODEL_PIP_EXTRA_INDEX_URL", dockerfile)
        self.assertIn('${MODEL_PIP_EXTRA_INDEX_URL:+--extra-index-url "$MODEL_PIP_EXTRA_INDEX_URL"}', dockerfile)

    def test_kaniko_cache_repo_without_namespace(self) -> None:
        manifest = build_job_manifest(
            job_name="build-model-tiny-demo",
            namespace="api-models",
            model_name="tiny_demo_model",
            model_version="latest",
            model_uri="",
            mlflow_tracking_uri="http://mlflow:5000",
            target_image="registry.local:5000/model-tiny-demo:v1",
            base_image="registry.local:5000/model-service-base:py312",
            registry_secret="model-registry-creds",
            kaniko_image="gcr.io/kaniko-project/executor:v1.23.2-debug",
            insecure_registry_tls=False,
            registry_server="registry.local:5000",
            kaniko_cache=True,
        )
        kaniko = next(
            c for c in manifest["spec"]["template"]["spec"]["containers"] if c["name"] == "kaniko"
        )
        self.assertIn("--cache-repo=registry.local:5000/kaniko-cache", kaniko["args"])

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

    def test_deployment_manifest_records_run_and_experiment(self) -> None:
        run_id = "0123456789abcdef0123456789abcdef"
        manifest = build_deployment_manifest(
            namespace="api-models",
            model_name="tiny_demo_model",
            model_slug="tiny-demo-model",
            model_image="registry.example.com/api-models/model-tiny-demo:latest",
            replicas=1,
            tracking_uri="http://mlflow:5000",
            model_uri="/models/model",
            model_version="image",
            registry_secret="model-registry-creds",
            model_service_prefix="model",
            run_id=run_id,
            source_model_uri=f"runs:/{run_id}/ludwig-model",
            experiment_name="Churn Prediction / EU",
            operation_id="op_20261123101500_a1b2c3",
        )
        selector = {"app": "model-service", "model-slug": "tiny-demo-model"}
        expected_labels = {
            **selector,
            "bentloy.io/run-id": run_id,
            "bentloy.io/experiment": "churn-prediction-eu",
        }
        expected_annotations = {
            "bentloy.io/model-uri": f"runs:/{run_id}/ludwig-model",
            "bentloy.io/experiment-name": "Churn Prediction / EU",
        }
        # Version metadata on the resource and the pods, never in the immutable selector.
        self.assertEqual(manifest["metadata"]["labels"], expected_labels)
        # The owning operation is on the Deployment only: a pod-template change would
        # restart the pods.
        self.assertEqual(
            manifest["metadata"]["annotations"],
            {**expected_annotations, "bentloy.io/operation-id": "op_20261123101500_a1b2c3"},
        )
        self.assertEqual(manifest["spec"]["selector"]["matchLabels"], selector)
        self.assertEqual(manifest["spec"]["template"]["metadata"]["labels"], expected_labels)
        self.assertEqual(
            manifest["spec"]["template"]["metadata"]["annotations"], expected_annotations
        )
        # The container still loads the model baked into the image.
        env = {e["name"]: e["value"] for e in manifest["spec"]["template"]["spec"]["containers"][0]["env"]}
        self.assertEqual(env["MODEL_URI"], "/models/model")

    def test_service_manifest_records_run_and_experiment(self) -> None:
        run_id = "0123456789abcdef0123456789abcdef"
        manifest = build_service_manifest(
            namespace="api-models",
            model_slug="tiny-demo-model",
            model_service_prefix="model",
            run_id=run_id,
            source_model_uri=f"runs:/{run_id}/ludwig-model",
            experiment_name="X" * 80,
            operation_id="op_1",
        )
        labels = manifest["metadata"]["labels"]
        self.assertEqual(labels["bentloy.io/run-id"], run_id)
        self.assertEqual(labels["bentloy.io/experiment"], "x" * 63)
        self.assertEqual(manifest["metadata"]["annotations"]["bentloy.io/experiment-name"], "X" * 80)
        self.assertEqual(manifest["metadata"]["annotations"]["bentloy.io/operation-id"], "op_1")
        self.assertEqual(
            manifest["spec"]["selector"],
            {"app": "model-service", "model-slug": "tiny-demo-model"},
        )

    def test_metadata_keys_are_always_present(self) -> None:
        # Empty values overwrite a previous run's labels when the resource is merge-patched.
        manifest = build_service_manifest(
            namespace="api-models",
            model_slug="tiny-demo-model",
            model_service_prefix="model",
        )
        self.assertEqual(manifest["metadata"]["labels"]["bentloy.io/run-id"], "")
        self.assertEqual(manifest["metadata"]["labels"]["bentloy.io/experiment"], "")
        self.assertEqual(
            manifest["metadata"]["annotations"],
            {
                "bentloy.io/model-uri": "",
                "bentloy.io/experiment-name": "",
                "bentloy.io/operation-id": "",
            },
        )


if __name__ == "__main__":
    unittest.main()
