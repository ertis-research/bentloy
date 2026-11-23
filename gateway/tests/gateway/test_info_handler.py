import unittest

from gateway.app.deploy.operation_store import DeployOperationStore
from gateway.app.handlers.info_handler import InfoHandler, ListModelsRequestError, paginate
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
        result = await handler.list_models(page=1, page_size=10, query="")

        self.assertEqual(result["discovery"]["status"], "error")
        self.assertEqual(result["discovery"]["error"], "simulated discovery error")
        self.assertEqual(result["configured_models"], ["a"])
        self.assertEqual(result["recently_routed_models"], ["z"])
        self.assertEqual(len(result["deploy_operations"]), 1)
        self.assertEqual(result["pagination"]["configured_models"]["total_items"], 1)
        self.assertFalse(result["pagination"]["configured_models"]["has_next"])

        filtered = await handler.list_models(page=1, page_size=10, query="zz")
        self.assertEqual(filtered["configured_models"], [])
        self.assertEqual(filtered["recently_routed_models"], [])

        second_page = await handler.list_models(page=2, page_size=1, query="")
        self.assertEqual(second_page["configured_models"], [])
        self.assertTrue(second_page["pagination"]["configured_models"]["has_previous"])

    async def test_list_models_filters_by_experiment_and_run(self) -> None:
        run_a1, run_a2, run_b = "a1" * 16, "a2" * 16, "b1" * 16

        def service(name: str, run_id, experiment) -> dict:
            return {
                "service_name": f"model-{name}",
                "model_slug": name,
                "inferred_model_name": name.replace("-", "_"),
                "base_url": f"http://model-{name}:3000",
                "predict_endpoint": f"http://model-{name}:3000/predict",
                "run_id": run_id,
                "model_uri": f"runs:/{run_id}/ludwig-model" if run_id else None,
                "experiment_name": experiment,
            }

        services = [
            service("churn-v1", run_a1, "Churn"),
            service("churn-v2", run_a2, "Churn"),
            service("churn-eu", run_b, "Churn EU"),
            service("legacy", None, None),
        ]

        store = DeployOperationStore(now_iso=lambda: "2026-04-15T12:00:00+00:00")
        for op_id, run_id, experiment in (
            ("op_a1", run_a1, "Churn"),
            ("op_a2", run_a2, "Churn"),
            ("op_b", run_b, "Churn EU"),
        ):
            await store.add(
                {  # type: ignore[typeddict-item]
                    "operation_id": op_id,
                    "status": "ready",
                    "updated_at": f"2026-04-15T12:00:0{len(op_id)}+00:00",
                    "model_name": op_id,
                    "deployed_name": op_id,
                    "image_ref": "registry/org/img:tag",
                    "run_id": run_id,
                    "model_uri": f"runs:/{run_id}/ludwig-model",
                    "experiment_name": experiment,
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
            resolve_k8s_auth=lambda: ("https://k8s", {}, False),
        )

        async def fake_discovery():
            return services, None

        handler.discover_k8s_model_services = fake_discovery  # type: ignore[method-assign]

        # No filter: everything, as before.
        result = await handler.list_models(page=1, page_size=10, query="")
        self.assertEqual(len(result["discovered_models"]), 4)
        self.assertEqual(len(result["deploy_operations"]), 3)
        self.assertEqual(result["configured_models"], ["a"])

        # Exact experiment filter: "Churn" does not match "Churn EU".
        result = await handler.list_models(page=1, page_size=10, query="", experiment_name="Churn")
        self.assertEqual(
            [svc["service_name"] for svc in result["discovered_models"]],
            ["model-churn-v1", "model-churn-v2"],
        )
        self.assertEqual(
            sorted(op["operation_id"] for op in result["deploy_operations"]), ["op_a1", "op_a2"]
        )
        self.assertEqual(result["configured_models"], [])
        self.assertEqual(result["recently_routed_models"], [])
        self.assertEqual(
            sorted(result["endpoints"]), ["churn_v1", "churn_v2"]
        )
        self.assertEqual(result["pagination"]["discovered_models"]["total_items"], 2)

        # Pagination applies to the filtered result.
        page_2 = await handler.list_models(page=2, page_size=1, query="", experiment_name="Churn")
        self.assertEqual(
            [svc["service_name"] for svc in page_2["discovered_models"]], ["model-churn-v2"]
        )
        self.assertFalse(page_2["pagination"]["discovered_models"]["has_next"])

        # Run filter (one version), combinable with the experiment filter.
        result = await handler.list_models(page=1, page_size=10, query="", run_id=run_a2)
        self.assertEqual(
            [svc["service_name"] for svc in result["discovered_models"]], ["model-churn-v2"]
        )
        self.assertEqual([op["operation_id"] for op in result["deploy_operations"]], ["op_a2"])
        result = await handler.list_models(
            page=1, page_size=10, query="", experiment_name="Churn EU", run_id=run_a2
        )
        self.assertEqual(result["discovered_models"], [])
        self.assertEqual(result["deploy_operations"], [])

    async def test_query_filters_deploy_operations_by_deployed_name(self) -> None:
        store = DeployOperationStore(now_iso=lambda: "2026-04-15T12:00:00+00:00")
        for op_id, deployed_name in (("op_1", "E2E_QA"), ("op_2", "e2e_qb"), ("op_3", "churn")):
            await store.add(
                {  # type: ignore[typeddict-item]
                    "operation_id": op_id,
                    "status": "ready",
                    "updated_at": "2026-04-15T12:00:00+00:00",
                    "model_name": "base_model",
                    "deployed_name": deployed_name,
                    "image_ref": "registry/org/img:tag",
                }
            )
        handler = InfoHandler(
            model_urls={},
            model_service_prefix="model",
            model_namespace="api-models",
            model_url_template="http://{service_name}:3000",
            recently_routed_models={},
            model_discovery=_FakeDiscovery(),
            operation_store=store,
            resolve_k8s_auth=lambda: ("https://k8s", {}, False),
        )

        async def no_services():
            return [], None

        handler.discover_k8s_model_services = no_services  # type: ignore[method-assign]

        result = await handler.list_models(page=1, page_size=10, query="e2e_q")
        self.assertEqual(
            sorted(op["operation_id"] for op in result["deploy_operations"]), ["op_1", "op_2"]
        )
        self.assertEqual(result["pagination"]["deploy_operations"]["total_items"], 2)
        result = await handler.list_models(page=1, page_size=10, query="e2e_qa")
        self.assertEqual([op["operation_id"] for op in result["deploy_operations"]], ["op_1"])
        result = await handler.list_models(page=1, page_size=10, query="")
        self.assertEqual(len(result["deploy_operations"]), 3)

    def _paged_handler(self, services: list[dict]) -> InfoHandler:
        """Handler with 3 configured models, 3 discovered services and no operations."""

        handler = InfoHandler(
            model_urls={name: f"http://model-{name}:3000" for name in ("a", "b", "c")},
            model_service_prefix="model",
            model_namespace="api-models",
            model_url_template="http://{service_name}:3000",
            recently_routed_models={},
            model_discovery=_FakeDiscovery(),
            operation_store=DeployOperationStore(),
            resolve_k8s_auth=lambda: ("https://k8s", {}, False),
        )

        async def fake_discovery():
            return services, None

        handler.discover_k8s_model_services = fake_discovery  # type: ignore[method-assign]
        return handler

    @staticmethod
    def _services(*names: str) -> list[dict]:
        return [
            {
                "service_name": f"model-{name}",
                "inferred_model_name": name,
                "predict_endpoint": f"http://model-{name}:3000/predict",
            }
            for name in names
        ]

    async def test_pages_are_one_indexed(self) -> None:
        handler = self._paged_handler(self._services("x", "y", "z"))

        # page=1 returns the first items of every list.
        first = await handler.list_models(page=1, page_size=2, query="")
        self.assertEqual(first["configured_models"], ["a", "b"])
        self.assertEqual([s["service_name"] for s in first["discovered_models"]], ["model-x", "model-y"])
        self.assertEqual(list(first["endpoints"]), ["x", "y"])
        meta = first["pagination"]["configured_models"]
        self.assertEqual(meta["current_page"], 1)
        self.assertEqual(meta["total_pages"], 2)
        self.assertEqual(meta["total_items"], 3)
        self.assertTrue(meta["has_next"])
        self.assertFalse(meta["has_previous"])

        # page=2 returns the next items and is the last page.
        second = await handler.list_models(page=2, page_size=2, query="")
        self.assertEqual(second["configured_models"], ["c"])
        self.assertEqual([s["service_name"] for s in second["discovered_models"]], ["model-z"])
        self.assertEqual(list(second["endpoints"]), ["z", "a"])
        for key in ("configured_models", "discovered_models"):
            meta = second["pagination"][key]
            self.assertEqual(meta["current_page"], 2, key)
            self.assertFalse(meta["has_next"], key)
            self.assertTrue(meta["has_previous"], key)
        # endpoints has 6 items (3 discovered + 3 configured): page 2 of 3.
        self.assertTrue(second["pagination"]["endpoints"]["has_next"])

    async def test_out_of_range_page_metadata(self) -> None:
        handler = self._paged_handler([])

        result = await handler.list_models(page=5, page_size=2, query="")
        pagination = result["pagination"]
        # Out of range: empty slice (not clamped to the last page), no next, previous exists.
        self.assertEqual(result["configured_models"], [])
        self.assertEqual(result["endpoints"], {})
        self.assertEqual(pagination["configured_models"]["current_page"], 5)
        self.assertEqual(pagination["configured_models"]["total_pages"], 2)
        self.assertFalse(pagination["configured_models"]["has_next"])
        self.assertTrue(pagination["configured_models"]["has_previous"])

    async def test_empty_list_has_zero_pages(self) -> None:
        handler = self._paged_handler([])

        result = await handler.list_models(page=1, page_size=2, query="")
        for key in ("recently_routed_models", "discovered_models", "deploy_operations"):
            meta = result["pagination"][key]
            self.assertEqual(meta["total_items"], 0, key)
            self.assertEqual(meta["total_pages"], 0, key)
            self.assertFalse(meta["has_next"], key)
            self.assertFalse(meta["has_previous"], key)

    async def test_page_below_one_is_rejected(self) -> None:
        handler = self._paged_handler([])

        for page in (0, -1):
            with self.assertRaises(ListModelsRequestError) as caught:
                await handler.list_models(page=page, page_size=2, query="")
            self.assertEqual(caught.exception.status_code, 400)
            self.assertIn("page must be >= 1", caught.exception.message)


class PaginateTest(unittest.TestCase):
    def test_slices_and_metadata(self) -> None:
        items = list(range(5))
        self.assertEqual(paginate(items, 1, 2)[0], [0, 1])
        self.assertEqual(paginate(items, 3, 2)[0], [4])
        self.assertEqual(paginate(items, 4, 2)[0], [])
        meta = paginate(items, 3, 2)[1]
        self.assertEqual(
            meta,
            {
                "total_items": 5,
                "total_pages": 3,
                "current_page": 3,
                "page_size": 2,
                "has_next": False,
                "has_previous": True,
            },
        )
        self.assertEqual(paginate([], 1, 10)[1]["total_pages"], 0)


if __name__ == "__main__":
    unittest.main()
