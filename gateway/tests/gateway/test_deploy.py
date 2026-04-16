import asyncio
import unittest
from unittest.mock import AsyncMock

import httpx

from gateway.app.deploy.operation_store import DeployOperationStore
from gateway.app.deploy.orchestrator import DeployOrchestrator
from gateway.app.domain.types import DeployOperationRecord, DeployOperationRequest
from gateway.service import GatewayService


def _build_request(job_name: str = "build-model-tiny-demo-abc123") -> DeployOperationRequest:
    return {
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
        "job_name": job_name,
    }


def _build_operation(
    operation_id: str,
    request: DeployOperationRequest,
    updated_at: str = "2026-04-15T10:00:00+00:00",
) -> DeployOperationRecord:
    return {
        "operation_id": operation_id,
        "status": "queued",
        "step": "queued",
        "created_at": "2026-04-15T10:00:00+00:00",
        "updated_at": updated_at,
        "model_name": request["model_name"],
        "deployed_name": request["deployed_name"],
        "model_version": request["model_version"],
        "namespace": request["namespace"],
        "replicas": request["replicas"],
        "image_ref": request["target_image"],
        "k8s_job": request["job_name"],
        "error": None,
        "message": None,
        "request": request,
    }


class _FakeKubernetesApiClient:
    def __init__(self):
        self.ensure_calls: list[dict[str, str]] = []
        self.wait_calls: list[dict[str, str | int]] = []

    async def read_job_logs(
        self,
        client: httpx.AsyncClient,
        base_url: str,
        headers: dict[str, str],
        namespace: str,
        job_name: str,
        container: str,
    ) -> str:
        return ""

    async def ensure_resource(
        self,
        client: httpx.AsyncClient,
        headers: dict[str, str],
        list_url: str,
        resource_url: str,
        manifest: dict,
    ) -> None:
        self.ensure_calls.append({"list_url": list_url, "resource_url": resource_url})

    async def wait_deployment_ready(
        self,
        client: httpx.AsyncClient,
        headers: dict[str, str],
        deployment_url: str,
        replicas: int,
        timeout_sec: int,
    ) -> None:
        self.wait_calls.append(
            {
                "deployment_url": deployment_url,
                "replicas": replicas,
                "timeout_sec": timeout_sec,
            }
        )


class DeployOperationStoreTest(unittest.IsolatedAsyncioTestCase):
    async def test_update_and_summary_sorting(self) -> None:
        timestamps = iter(
            [
                "2026-04-15T10:00:01+00:00",
                "2026-04-15T10:00:02+00:00",
            ]
        )
        store = DeployOperationStore(now_iso=lambda: next(timestamps))

        op_a = _build_operation(
            operation_id="op_a",
            request=_build_request("build-model-a"),
            updated_at="2026-04-15T10:00:00+00:00",
        )
        op_b = _build_operation(
            operation_id="op_b",
            request=_build_request("build-model-b"),
            updated_at="2026-04-15T09:59:59+00:00",
        )

        await store.add(op_a)
        await store.add(op_b)
        await store.update("op_b", status="building", step="create_job")

        updated = await store.get("op_b")
        self.assertIsNotNone(updated)
        assert updated is not None
        self.assertEqual(updated["status"], "building")
        self.assertEqual(updated["updated_at"], "2026-04-15T10:00:01+00:00")

        summaries = await store.list_summaries(limit=2)
        self.assertEqual([item["operation_id"] for item in summaries], ["op_b", "op_a"])


class DeployOrchestratorTest(unittest.IsolatedAsyncioTestCase):
    async def test_run_operation_ready(self) -> None:
        store = DeployOperationStore(now_iso=lambda: "2026-04-15T11:00:00+00:00")
        request = _build_request()
        operation = _build_operation("op_ready", request)
        await store.add(operation)

        fake_k8s = _FakeKubernetesApiClient()

        def handler(request_obj: httpx.Request) -> httpx.Response:
            if (
                request_obj.method == "POST"
                and request_obj.url.path == "/apis/batch/v1/namespaces/api-models/jobs"
            ):
                return httpx.Response(status_code=201, json={"kind": "Job"})
            if (
                request_obj.method == "GET"
                and request_obj.url.path
                == "/apis/batch/v1/namespaces/api-models/jobs/build-model-tiny-demo-abc123"
            ):
                return httpx.Response(status_code=200, json={"status": {"succeeded": 1}})
            raise AssertionError(f"Unexpected request: {request_obj.method} {request_obj.url}")

        transport = httpx.MockTransport(handler)
        orchestrator = DeployOrchestrator(
            operation_store=store,
            k8s_client=fake_k8s,  # type: ignore[arg-type]
            resolve_k8s_auth=lambda: (
                "https://kubernetes.default.svc",
                {"Authorization": "Bearer test"},
                False,
            ),
            build_job_manifest=lambda **kwargs: {"metadata": {"name": kwargs["job_name"]}},
            build_deployment_manifest=lambda **kwargs: {
                "metadata": {"name": f"model-{kwargs['model_slug']}"}
            },
            build_service_manifest=lambda namespace, model_slug, model_service_prefix: {
                "metadata": {"name": f"model-{model_slug}"}
            },
            kaniko_image="gcr.io/kaniko-project/executor:v1.23.2-debug",
            model_service_prefix="model",
            build_timeout_sec=10,
            rollout_timeout_sec=10,
            http_client_factory=lambda verify: httpx.AsyncClient(
                transport=transport,
                timeout=20.0,
                verify=False,
            ),
        )

        await orchestrator.run_operation("op_ready")
        result = await store.get("op_ready")
        self.assertIsNotNone(result)
        assert result is not None
        self.assertEqual(result["status"], "ready")
        self.assertEqual(result["step"], "completed")
        self.assertEqual(result["message"], "Deployment completed successfully.")
        self.assertEqual(len(fake_k8s.ensure_calls), 2)
        self.assertEqual(len(fake_k8s.wait_calls), 1)

    async def test_run_operation_job_creation_error_marks_failed(self) -> None:
        store = DeployOperationStore(now_iso=lambda: "2026-04-15T11:05:00+00:00")
        request = _build_request(job_name="build-model-fail-abc123")
        operation = _build_operation("op_failed", request)
        await store.add(operation)

        fake_k8s = _FakeKubernetesApiClient()

        def handler(request_obj: httpx.Request) -> httpx.Response:
            if (
                request_obj.method == "POST"
                and request_obj.url.path == "/apis/batch/v1/namespaces/api-models/jobs"
            ):
                return httpx.Response(status_code=500, text="boom")
            raise AssertionError(f"Unexpected request: {request_obj.method} {request_obj.url}")

        transport = httpx.MockTransport(handler)
        orchestrator = DeployOrchestrator(
            operation_store=store,
            k8s_client=fake_k8s,  # type: ignore[arg-type]
            resolve_k8s_auth=lambda: (
                "https://kubernetes.default.svc",
                {"Authorization": "Bearer test"},
                False,
            ),
            build_job_manifest=lambda **kwargs: {"metadata": {"name": kwargs["job_name"]}},
            build_deployment_manifest=lambda **kwargs: {
                "metadata": {"name": f"model-{kwargs['model_slug']}"}
            },
            build_service_manifest=lambda namespace, model_slug, model_service_prefix: {
                "metadata": {"name": f"model-{model_slug}"}
            },
            kaniko_image="gcr.io/kaniko-project/executor:v1.23.2-debug",
            model_service_prefix="model",
            build_timeout_sec=10,
            rollout_timeout_sec=10,
            http_client_factory=lambda verify: httpx.AsyncClient(
                transport=transport,
                timeout=20.0,
                verify=False,
            ),
        )

        await orchestrator.run_operation("op_failed")
        result = await store.get("op_failed")
        self.assertIsNotNone(result)
        assert result is not None
        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["step"], "failed")
        self.assertIsNotNone(result["error"])
        self.assertIn("Job creation failed (500)", result["error"] or "")


class GatewayDeployApiTest(unittest.IsolatedAsyncioTestCase):
    async def test_deploy_model_and_deploy_status_contract(self) -> None:
        service = GatewayService()
        service.model_base_image = "registry.example.com/api-models/model-service-base:py310"
        service.deploy_handler.model_base_image = service.model_base_image
        service.deploy_orchestrator.run_operation = AsyncMock(return_value=None)

        response = await service.deploy_model(
            model_name="tiny_demo_model",
            model_version="latest",
            tracking_uri="http://mlflow:5000",
            target_image="registry.example.com/api-models/model-tiny-demo:latest",
        )
        self.assertIn("operation_id", response)
        self.assertIn("k8s_job", response)
        self.assertEqual(response["deployed_name"], "tiny_demo_model")

        operation_id = response["operation_id"]
        await asyncio.sleep(0)
        status = await service.deploy_status(operation_id)
        self.assertEqual(status["operation_id"], operation_id)
        self.assertEqual(status["status"], "queued")
        self.assertEqual(status["deployed_name"], "tiny_demo_model")

        await service.operation_store.update(operation_id, status="building", step="create_job")
        updated_status = await service.deploy_status(operation_id)
        self.assertEqual(updated_status["status"], "building")
        self.assertEqual(updated_status["step"], "create_job")

    async def test_deploy_model_accepts_custom_deployed_name(self) -> None:
        service = GatewayService()
        service.model_base_image = "registry.example.com/api-models/model-service-base:py310"
        service.deploy_handler.model_base_image = service.model_base_image
        service.deploy_orchestrator.run_operation = AsyncMock(return_value=None)

        response = await service.deploy_model(
            model_name="tiny_demo_model",
            model_version="latest",
            tracking_uri="http://mlflow:5000",
            target_image="registry.example.com/api-models/model-tiny-demo:latest",
            deployed_name="tiny_alias",
            append_date=True,
        )

        self.assertRegex(response["deployed_name"], r"^tiny_alias_[0-9]{8}$")
        self.assertRegex(response["k8s_job"], r"^build-model-tiny-alias-[0-9]{8}-[a-z0-9]{6}$")

        operation_id = response["operation_id"]
        status = await service.deploy_status(operation_id)
        self.assertRegex(status["deployed_name"], r"^tiny_alias_[0-9]{8}$")


if __name__ == "__main__":
    unittest.main()
