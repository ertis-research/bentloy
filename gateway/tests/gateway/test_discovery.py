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
        # Services created before run/experiment metadata existed.
        self.assertIsNone(services[0]["run_id"])
        self.assertIsNone(services[0]["model_uri"])
        self.assertIsNone(services[0]["experiment_name"])

    async def test_discover_services_newest_first(self) -> None:
        payload = {
            "items": [
                {"metadata": {"name": "model-old", "creationTimestamp": "2026-01-01T10:00:00Z"}},
                {"metadata": {"name": "model-undated"}},
                {"metadata": {"name": "model-new", "creationTimestamp": "2026-03-01T10:00:00Z"}},
                {"metadata": {"name": "model-b-same", "creationTimestamp": "2026-02-01T10:00:00Z"}},
                {"metadata": {"name": "model-a-same", "creationTimestamp": "2026-02-01T10:00:00Z"}},
            ]
        }
        discovery = KubernetesModelDiscovery(
            model_service_prefix="model",
            model_namespace="api-models",
            model_url_template="http://{service_name}:3000",
            client_factory=lambda **kwargs: _FakeAsyncClient(_FakeResponse(200, payload)),
        )

        services, error = await discovery.discover_services(
            base_url="https://kubernetes", headers={}, verify=False
        )
        self.assertIsNone(error)
        self.assertEqual(
            [svc["service_name"] for svc in services],
            ["model-new", "model-a-same", "model-b-same", "model-old", "model-undated"],
        )
        self.assertEqual(services[0]["created_at"], "2026-03-01T10:00:00Z")
        self.assertIsNone(services[-1]["created_at"])

    async def test_discover_services_reads_run_and_experiment(self) -> None:
        run_id = "0123456789abcdef0123456789abcdef"
        payload = {
            "items": [
                {
                    "metadata": {
                        "name": "model-churn-v2",
                        "labels": {
                            "app": "model-service",
                            "model-slug": "churn-v2",
                            "bentloy.io/run-id": run_id,
                            "bentloy.io/experiment": "churn-prediction",
                        },
                        "annotations": {
                            "bentloy.io/model-uri": f"runs:/{run_id}/ludwig-model",
                            "bentloy.io/experiment-name": "Churn Prediction",
                        },
                    }
                },
                {
                    "metadata": {
                        "name": "model-redeployed-without-run",
                        "labels": {"bentloy.io/run-id": ""},
                        "annotations": {"bentloy.io/model-uri": ""},
                    }
                },
            ]
        }
        discovery = KubernetesModelDiscovery(
            model_service_prefix="model",
            model_namespace="api-models",
            model_url_template="http://{service_name}:3000",
            client_factory=lambda **kwargs: _FakeAsyncClient(_FakeResponse(200, payload)),
        )

        services, error = await discovery.discover_services(
            base_url="https://kubernetes", headers={}, verify=False
        )
        self.assertIsNone(error)
        by_name = {svc["service_name"]: svc for svc in services}
        churn = by_name["model-churn-v2"]
        self.assertEqual(churn["run_id"], run_id)
        self.assertEqual(churn["model_uri"], f"runs:/{run_id}/ludwig-model")
        self.assertEqual(churn["experiment_name"], "Churn Prediction")
        self.assertEqual(churn["predict_endpoint"], "http://model-churn-v2:3000/predict")
        empty = by_name["model-redeployed-without-run"]
        self.assertIsNone(empty["run_id"])
        self.assertIsNone(empty["model_uri"])
        self.assertIsNone(empty["experiment_name"])

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
