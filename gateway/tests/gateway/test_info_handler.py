import unittest

from gateway.app.deploy.operation_store import DeployOperationStore
from gateway.app.handlers.info_handler import InfoHandler
from gateway.app.routing.discovery import KubernetesModelDiscovery


class _FakeResponse:
    def __init__(self, payload: dict):
        self._payload = payload

    def json(self) -> dict:
        return self._payload


class _FakeHealthClient:
    def __init__(self):
        self.fail_urls: set[str] = set()
        self.responses: dict[str, dict] = {}

    async def __aenter__(self) -> "_FakeHealthClient":
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        return None

    async def get(self, url: str):
        if url in self.fail_urls:
            raise RuntimeError("health endpoint not reachable")
        return _FakeResponse(self.responses.get(url, {"status": "ok"}))


class _FakeDiscovery(KubernetesModelDiscovery):
    def __init__(self):
        pass

    async def discover_services(
        self,
        base_url: str,
        headers: dict[str, str],
        verify,
    ) -> tuple[list[dict[str, str]], str | None]:
        return (
            [
                {
                    "service_name": "model-tiny-demo-model",
                    "inferred_model_name": "tiny_demo_model",
                    "predict_endpoint": "http://model-tiny-demo-model:3000/predict",
                }
            ],
            None,
        )


class InfoHandlerTest(unittest.IsolatedAsyncioTestCase):
    async def test_health_contract(self) -> None:
        fake_client = _FakeHealthClient()
        fake_client.responses["http://model-a:3000/health"] = {"status": "healthy"}
        fake_client.fail_urls.add("http://model-b:3000/health")

        handler = InfoHandler(
            model_urls={
                "a": "http://model-a:3000",
                "b": "http://model-b:3000",
            },
            model_service_prefix="model",
            model_namespace="api-models",
            model_url_template="http://{service_name}:3000",
            recently_routed_models={},
            model_discovery=_FakeDiscovery(),
            operation_store=DeployOperationStore(),
            resolve_k8s_auth=lambda: ("https://k8s", {"Authorization": "Bearer x"}, False),
            client_factory=lambda timeout: fake_client,
        )

        result = await handler.health()

        self.assertEqual(result["gateway"], "healthy")
        self.assertEqual(result["models"]["a"], {"status": "healthy"})
        self.assertEqual(result["models"]["b"]["status"], "unhealthy")

    async def test_list_models_contract_and_discovery_error(self) -> None:
        store = DeployOperationStore(now_iso=lambda: "2026-04-15T12:00:00+00:00")
        await store.add(
            {
                "operation_id": "op_1",
                "status": "queued",
                "step": "queued",
                "created_at": "2026-04-15T11:59:59+00:00",
                "updated_at": "2026-04-15T11:59:59+00:00",
                "model_name": "tiny_demo_model",
                "deployed_name": "tiny_demo_model",
                "model_version": "latest",
                "namespace": "api-models",
                "replicas": 1,
                "image_ref": "registry.example.com/api-models/model-tiny-demo:latest",
                "k8s_job": "build-model-tiny-demo",
                "error": None,
                "message": None,
                "request": {
                    "namespace": "api-models",
                    "model_name": "tiny_demo_model",
                    "model_slug": "tiny-demo-model",
                    "deployed_name": "tiny_demo_model",
                    "deployment_slug": "tiny-demo-model",
                    "model_version": "latest",
                    "model_uri": "",
                    "tracking_uri": "http://mlflow:5000",
                    "replicas": 1,
                    "target_image": "registry.example.com/api-models/model-tiny-demo:latest",
                    "base_image": "registry.example.com/api-models/model-service-base:py310",
                    "registry_secret": "model-registry-creds",
                    "insecure_registry_tls": False,
                    "registry_server": "registry.example.com",
                    "job_name": "build-model-tiny-demo",
                },
            }
        )

        handler = InfoHandler(
            model_urls={"a": "http://model-a:3000"},
            model_service_prefix="model",
            model_namespace="api-models",
            model_url_template="http://{service_name}:3000",
            recently_routed_models={"z": "http://model-z:3000"},
            model_discovery=_FakeDiscovery(),
            operation_store=store,
            resolve_k8s_auth=lambda: ("https://k8s", {"Authorization": "Bearer x"}, False),
            client_factory=lambda timeout: _FakeHealthClient(),
        )

        async def fake_discovery_error():
            return [], "simulated discovery error"

        handler.discover_k8s_model_services = fake_discovery_error  # type: ignore[method-assign]
        result = await handler.list_models()

        self.assertEqual(result["discovery"]["status"], "error")
        self.assertEqual(result["discovery"]["error"], "simulated discovery error")
        self.assertEqual(result["configured_models"], ["a"])
        self.assertEqual(result["recently_routed_models"], ["z"])
        self.assertEqual(len(result["deploy_operations"]), 1)


if __name__ == "__main__":
    unittest.main()
