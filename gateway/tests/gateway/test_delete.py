import unittest
from unittest.mock import AsyncMock

import httpx

from gateway.app.deploy.deploy_handler import DeployHandler
from gateway.app.deploy.operation_store import DeployOperationStore
from gateway.app.deploy.orchestrator import DeployOrchestrator
from gateway.app.kubernetes.client import KubernetesApiClient

NS = "api-models"
IMAGE = "reg.example.com/api-models/model-churn:vlatest-r0123abcd"
OTHER_IMAGE = "reg.example.com/api-models/model-churn:v2-r9999ffff"
DEPLOYMENTS = f"/apis/apps/v1/namespaces/{NS}/deployments"
SERVICES = f"/api/v1/namespaces/{NS}/services"
JOBS = f"/apis/batch/v1/namespaces/{NS}/jobs"


def _deployment(name: str, image: str, operation_id: str | None = None) -> dict:
    annotations = {"bentloy.io/operation-id": operation_id} if operation_id is not None else {}
    return {
        "metadata": {"name": name, "annotations": annotations},
        "spec": {"template": {"spec": {"containers": [{"name": "model", "image": image}]}}},
    }


def _service(name: str, operation_id: str | None = None) -> dict:
    annotations = {"bentloy.io/operation-id": operation_id} if operation_id is not None else {}
    return {"metadata": {"name": name, "annotations": annotations}}


class _FakeCluster:
    """In-memory Kubernetes API: GET/DELETE single resources and LIST deployments."""

    def __init__(self) -> None:
        self.resources: dict[str, dict] = {}
        self.deleted: list[str] = []

    def add_deployment(self, name: str, image: str, operation_id: str | None = None) -> None:
        self.resources[f"{DEPLOYMENTS}/{name}"] = _deployment(name, image, operation_id)

    def add_service(self, name: str, operation_id: str | None = None) -> None:
        self.resources[f"{SERVICES}/{name}"] = _service(name, operation_id)

    def handler(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if request.method == "GET" and path == DEPLOYMENTS:
            items = [r for p, r in self.resources.items() if p.startswith(DEPLOYMENTS + "/")]
            return httpx.Response(200, json={"items": items})
        if request.method == "GET":
            if path in self.resources:
                return httpx.Response(200, json=self.resources[path])
            return httpx.Response(404)
        if request.method == "DELETE":
            if self.resources.pop(path, None) is None:
                return httpx.Response(404)
            self.deleted.append(path)
            return httpx.Response(200, json={})
        return httpx.Response(405)


def _orchestrator(cluster: _FakeCluster, store: DeployOperationStore | None = None):
    orchestrator = DeployOrchestrator(
        operation_store=store or DeployOperationStore(),
        k8s_client=KubernetesApiClient(),
        resolve_k8s_auth=lambda: ("https://k8s", {}, False),
        build_job_manifest=lambda **_: {},
        build_delete_job_manifest=lambda **_: {},
        build_deployment_manifest=lambda **_: {},
        build_service_manifest=lambda **_: {},
        kaniko_image="kaniko",
        model_service_prefix="bentloy-model",
        build_timeout_sec=1,
        rollout_timeout_sec=1,
        http_client_factory=lambda verify: httpx.AsyncClient(
            transport=httpx.MockTransport(cluster.handler)
        ),
        registry_secret="creds",
    )
    orchestrator._delete_registry_image = AsyncMock(return_value=None)
    return orchestrator


def _operation(
    operation_id: str = "op_1",
    deployed_name: str = "Churn V2",
    deployment_slug: str = "churn-v2",
    image_ref: str = IMAGE,
    status: str = "ready",
) -> dict:
    return {
        "operation_id": operation_id,
        "status": status,
        "deployed_name": deployed_name,
        "namespace": NS,
        "image_ref": image_ref,
        "k8s_job": f"bentloy-build-{deployment_slug}-abc123",
        "request": {"namespace": NS, "deployment_slug": deployment_slug},
    }


class DeleteOperationResourcesTest(unittest.IsolatedAsyncioTestCase):
    async def test_owned_resources_are_deleted_by_resource_name(self) -> None:
        cluster = _FakeCluster()
        cluster.add_deployment("bentloy-model-churn-v2", IMAGE, "op_1")
        cluster.add_service("bentloy-model-churn-v2", "op_1")
        cluster.resources[f"{JOBS}/bentloy-build-churn-v2-abc123"] = {"metadata": {}}
        orchestrator = _orchestrator(cluster)

        notes = await orchestrator.delete_operation_resources(_operation(), "creds", False)

        self.assertEqual(
            cluster.deleted,
            [
                f"{DEPLOYMENTS}/bentloy-model-churn-v2",
                f"{SERVICES}/bentloy-model-churn-v2",
                f"{JOBS}/bentloy-build-churn-v2-abc123",
            ],
        )
        # The delete-image Job is named from the slug, never from the raw deployed_name.
        orchestrator._delete_registry_image.assert_awaited_once_with(
            image_ref=IMAGE,
            namespace=NS,
            deployment_slug="churn-v2",
            registry_secret="creds",
            insecure_registry_tls=False,
        )
        self.assertIn(f"Image {IMAGE} deleted.", notes)

    async def test_resources_owned_by_another_operation_are_kept(self) -> None:
        # op_1 failed or was overwritten: op_2 now owns the resource, with another image.
        cluster = _FakeCluster()
        cluster.add_deployment("bentloy-model-churn-v2", OTHER_IMAGE, "op_2")
        cluster.add_service("bentloy-model-churn-v2", "op_2")
        orchestrator = _orchestrator(cluster)

        notes = await orchestrator.delete_operation_resources(_operation(), "creds", False)

        self.assertEqual(cluster.deleted, [])
        self.assertIn("Deployment bentloy-model-churn-v2 kept: owned by op_2.", notes)
        self.assertIn("Service bentloy-model-churn-v2 kept: owned by op_2.", notes)
        # op_1's own image is not used by anyone: it is deleted.
        orchestrator._delete_registry_image.assert_awaited_once()

    async def test_resources_without_operation_id_are_kept(self) -> None:
        # Deployed before the annotation existed: cannot prove ownership, so keep it.
        cluster = _FakeCluster()
        cluster.add_deployment("bentloy-model-churn-v2", IMAGE)
        cluster.add_service("bentloy-model-churn-v2")
        orchestrator = _orchestrator(cluster)

        notes = await orchestrator.delete_operation_resources(_operation(), "creds", False)

        self.assertEqual(cluster.deleted, [])
        self.assertIn("a deploy without operation id", notes[0])
        # ...and its image is the one that Deployment runs: kept too.
        orchestrator._delete_registry_image.assert_not_awaited()
        self.assertIn("still used by Deployment bentloy-model-churn-v2", notes[-1])

    async def test_deployed_name_matching_another_workload_does_not_touch_it(self) -> None:
        cluster = _FakeCluster()
        cluster.add_deployment("bentloy-gateway", "reg.example.com/gateway:1")
        cluster.add_service("bentloy-gateway")
        orchestrator = _orchestrator(cluster)

        operation = _operation(deployed_name="bentloy-gateway", deployment_slug="bentloy-gateway")
        await orchestrator.delete_operation_resources(operation, "creds", False)

        self.assertIn(f"{DEPLOYMENTS}/bentloy-gateway", cluster.resources)
        self.assertIn(f"{SERVICES}/bentloy-gateway", cluster.resources)
        self.assertEqual(cluster.deleted, [])

    async def test_image_used_by_another_deployment_is_kept(self) -> None:
        cluster = _FakeCluster()
        cluster.add_deployment("bentloy-model-churn-v2", IMAGE, "op_1")
        cluster.add_service("bentloy-model-churn-v2", "op_1")
        cluster.add_deployment("bentloy-model-churn-prod", IMAGE, "op_9")
        orchestrator = _orchestrator(cluster)

        notes = await orchestrator.delete_operation_resources(_operation(), "creds", False)

        self.assertIn(f"{DEPLOYMENTS}/bentloy-model-churn-v2", cluster.deleted)
        orchestrator._delete_registry_image.assert_not_awaited()
        self.assertIn(
            f"Image {IMAGE} kept: still used by Deployment bentloy-model-churn-prod.", notes
        )

    async def test_image_of_a_deploy_in_progress_is_kept(self) -> None:
        store = DeployOperationStore()
        await store.add(_operation(operation_id="op_9", deployed_name="churn prod",
                                   deployment_slug="churn-prod", status="building"))
        cluster = _FakeCluster()
        orchestrator = _orchestrator(cluster, store)

        notes = await orchestrator.delete_operation_resources(_operation(), "creds", False)

        orchestrator._delete_registry_image.assert_not_awaited()
        self.assertIn("still used by operation op_9 (building)", notes[-1])

    async def test_finished_operations_do_not_keep_the_image(self) -> None:
        store = DeployOperationStore()
        await store.add(_operation(operation_id="op_9", status="failed"))
        await store.add(_operation(operation_id="op_1", status="deleting"))
        orchestrator = _orchestrator(_FakeCluster(), store)

        await orchestrator.delete_operation_resources(_operation(), "creds", False)

        orchestrator._delete_registry_image.assert_awaited_once()

    async def test_listing_failure_keeps_the_image(self) -> None:
        cluster = _FakeCluster()
        orchestrator = _orchestrator(cluster)
        orchestrator.k8s_client.list_resources = AsyncMock(side_effect=RuntimeError("List failed (403)"))

        with self.assertRaisesRegex(RuntimeError, "403"):
            await orchestrator.delete_operation_resources(_operation(), "creds", False)
        orchestrator._delete_registry_image.assert_not_awaited()


class DeleteDeployedModelTest(unittest.IsolatedAsyncioTestCase):
    async def test_rejects_names_outside_the_model_prefix(self) -> None:
        cluster = _FakeCluster()
        cluster.add_deployment("bentloy-gateway", "reg.example.com/gateway:1")
        orchestrator = _orchestrator(cluster)

        for name in ("bentloy-gateway", "kmlops", "bentloy-model-"):
            result = await orchestrator.delete_deployed_model(name, NS)
            self.assertEqual(result["status"], "error", name)
        self.assertEqual(cluster.deleted, [])

    async def test_deletes_deployment_service_and_unused_image(self) -> None:
        cluster = _FakeCluster()
        cluster.add_deployment("bentloy-model-churn-v2", IMAGE)
        cluster.add_service("bentloy-model-churn-v2")
        orchestrator = _orchestrator(cluster)

        result = await orchestrator.delete_deployed_model("bentloy-model-churn-v2", NS)

        self.assertEqual(result["status"], "success")
        self.assertEqual(
            cluster.deleted,
            [f"{DEPLOYMENTS}/bentloy-model-churn-v2", f"{SERVICES}/bentloy-model-churn-v2"],
        )
        orchestrator._delete_registry_image.assert_awaited_once_with(
            image_ref=IMAGE,
            namespace=NS,
            deployment_slug="churn-v2",
            registry_secret="creds",
            insecure_registry_tls=False,
        )

    async def test_keeps_an_image_another_deployment_uses(self) -> None:
        cluster = _FakeCluster()
        cluster.add_deployment("bentloy-model-churn-v2", IMAGE)
        cluster.add_service("bentloy-model-churn-v2")
        cluster.add_deployment("bentloy-model-churn-prod", IMAGE)
        orchestrator = _orchestrator(cluster)

        result = await orchestrator.delete_deployed_model("bentloy-model-churn-v2", NS)

        self.assertEqual(result["status"], "success")
        self.assertIn("still used by Deployment bentloy-model-churn-prod", result["message"])
        orchestrator._delete_registry_image.assert_not_awaited()
        self.assertIn(f"{DEPLOYMENTS}/bentloy-model-churn-prod", cluster.resources)

    async def test_orphan_service_is_deleted(self) -> None:
        cluster = _FakeCluster()
        cluster.add_service("bentloy-model-churn-v2")
        orchestrator = _orchestrator(cluster)

        result = await orchestrator.delete_deployed_model("bentloy-model-churn-v2", NS)

        self.assertEqual(cluster.deleted, [f"{SERVICES}/bentloy-model-churn-v2"])
        self.assertEqual(result["status"], "success")
        orchestrator._delete_registry_image.assert_not_awaited()

    async def test_unknown_model(self) -> None:
        orchestrator = _orchestrator(_FakeCluster())
        result = await orchestrator.delete_deployed_model("bentloy-model-nope", NS)
        self.assertEqual(result["status"], "error")
        self.assertIn("not found", result["message"])


class DeleteOperationHandlerTest(unittest.IsolatedAsyncioTestCase):
    def _handler(self, store: DeployOperationStore, orchestrator) -> DeployHandler:
        return DeployHandler(
            auto_deploy_enabled=True,
            model_namespace=NS,
            default_tracking_uri="http://mlflow:5000",
            kaniko_skip_tls_verify_default=False,
            registry_server="reg.example.com",
            registry_namespace=NS,
            model_base_image="reg.example.com/base:py312",
            registry_secret="gateway-creds",
            operation_store=store,
            deploy_orchestrator=orchestrator,
            now_iso=lambda: "2026-11-23T10:00:00+00:00",
        )

    async def test_success_removes_the_operation(self) -> None:
        store = DeployOperationStore()
        operation = _operation()
        operation["request"]["registry_secret"] = "op-creds"
        await store.add(operation)
        orchestrator = AsyncMock()
        orchestrator.delete_operation_resources.return_value = ["Deployment x deleted."]
        handler = self._handler(store, orchestrator)

        await handler.delete_operation_from_store("op_1", store, "gateway-creds")

        self.assertIsNone(await store.get("op_1"))
        orchestrator.delete_operation_resources.assert_awaited_once()
        kwargs = orchestrator.delete_operation_resources.await_args.kwargs
        self.assertEqual(kwargs["registry_secret"], "op-creds")

    async def test_failure_marks_delete_failed(self) -> None:
        store = DeployOperationStore()
        await store.add(_operation())
        orchestrator = AsyncMock()
        orchestrator.delete_operation_resources.side_effect = RuntimeError("Delete failed (403)")
        handler = self._handler(store, orchestrator)

        await handler.delete_operation_from_store("op_1", store, "gateway-creds")

        operation = await store.get("op_1")
        self.assertEqual(operation["status"], "delete_failed")
        self.assertIn("403", operation["error"])


if __name__ == "__main__":
    unittest.main()
