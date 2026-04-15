import unittest

import httpx

from gateway import service as gateway_entry
from gateway.app.routing.discovery import KubernetesModelDiscovery


class _FakeResponse:
    def __init__(self, status_code: int, payload: dict):
        self.status_code = status_code
        self._payload = payload

    def json(self) -> dict:
        return self._payload

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise httpx.HTTPStatusError(
                "error",
                request=httpx.Request("GET", "http://k8s"),
                response=httpx.Response(self.status_code),
            )


class _FakeAsyncClient:
    def __init__(self, response: _FakeResponse):
        self._response = response

    async def __aenter__(self) -> "_FakeAsyncClient":
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        return None

    async def get(self, url: str, headers: dict[str, str]) -> _FakeResponse:
        return self._response


class _BrokenAsyncClient:
    async def __aenter__(self) -> "_BrokenAsyncClient":
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        return None

    async def get(self, url: str, headers: dict[str, str]):
        raise RuntimeError("network error")


class DiscoveryTest(unittest.IsolatedAsyncioTestCase):
    async def test_discover_services_success(self) -> None:
        payload = {
            "items": [
                {"metadata": {"name": "model-tiny-demo-model"}},
                {"metadata": {"name": "gateway"}},
            ]
        }
        discovery = KubernetesModelDiscovery(
            model_service_prefix="model",
            model_namespace="api-models",
            model_url_template="http://{service_name}:3000",
            client_factory=lambda **kwargs: _FakeAsyncClient(
                _FakeResponse(200, payload)
            ),
        )

        services, error = await discovery.discover_services(
            base_url="https://kubernetes",
            headers={"Authorization": "Bearer token"},
            verify=False,
        )
        self.assertIsNone(error)
        self.assertEqual(len(services), 1)
        self.assertEqual(services[0]["service_name"], "model-tiny-demo-model")
        self.assertEqual(services[0]["inferred_model_name"], "tiny_demo_model")

    async def test_discover_services_error(self) -> None:
        discovery = KubernetesModelDiscovery(
            model_service_prefix="model",
            model_namespace="api-models",
            model_url_template="http://{service_name}:3000",
            client_factory=lambda **kwargs: _BrokenAsyncClient(),
        )
        services, error = await discovery.discover_services(
            base_url="https://kubernetes",
            headers={"Authorization": "Bearer token"},
            verify=False,
        )
        self.assertEqual(services, [])
        self.assertIsNotNone(error)
        self.assertIn("Could not discover Kubernetes services", error or "")


class ListModelsDiscoveryContractTest(unittest.IsolatedAsyncioTestCase):
    async def test_list_models_surfaces_discovery_error(self) -> None:
        gateway_class = gateway_entry.GatewayService.inner
        gateway = gateway_class()

        async def fake_discovery():
            return [], "simulated discovery error"

        gateway.info_handler.discover_k8s_model_services = fake_discovery  # type: ignore[method-assign]
        result = await gateway.list_models()

        self.assertEqual(result["discovery"]["status"], "error")
        self.assertEqual(result["discovery"]["error"], "simulated discovery error")
        self.assertEqual(result["discovered_models"], [])


if __name__ == "__main__":
    unittest.main()
