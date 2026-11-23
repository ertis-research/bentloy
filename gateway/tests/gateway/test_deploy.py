import asyncio
import unittest
from unittest.mock import AsyncMock

import httpx

from gateway.app.deploy.operation_store import DeployOperationStore
from gateway.app.deploy.orchestrator import DeployOrchestrator
from gateway.app.domain.types import DeployOperationRecord, DeployOperationRequest
from gateway.app.kubernetes.client import KubernetesApiClient
from gateway.service import GatewayService

RUN_ID = "0123456789abcdef0123456789abcdef"


def _build_request(job_name: str = "build-model-tiny-demo-abc123") -> DeployOperationRequest:
    return {
        "namespace": "api-models",
        "model_name": "tiny_demo_model",
        "model_slug": "tiny-demo-model",
        "deployed_name": "tiny_demo_model",
        "deployment_slug": "tiny-demo-model",
        "model_version": "latest",
        "run_id": RUN_ID,
        "experiment_name": "Tiny Demo",
        "model_uri": f"runs:/{RUN_ID}/ludwig-model",
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
        "run_id": request["run_id"],
        "model_uri": request["model_uri"],
        "experiment_name": request["experiment_name"],
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
        self.assertEqual(summaries[0]["run_id"], RUN_ID)
        self.assertEqual(summaries[0]["model_uri"], f"runs:/{RUN_ID}/ludwig-model")
        self.assertEqual(summaries[0]["experiment_name"], "Tiny Demo")

    async def test_list_summaries_filters_by_experiment_and_run(self) -> None:
        store = DeployOperationStore(now_iso=lambda: "2026-04-15T10:00:00+00:00")
        other_run = "f" * 32
        other_request = {
            **_build_request("build-model-b"),
            "run_id": other_run,
            "experiment_name": "Other",
        }
        await store.add(_build_operation("op_a", _build_request("build-model-a")))
        await store.add(_build_operation("op_b", other_request))  # type: ignore[arg-type]
        legacy = _build_operation("op_c", _build_request("build-model-c"))
        for key in ("run_id", "model_uri", "experiment_name"):
            legacy.pop(key)  # type: ignore[misc]
        await store.add(legacy)

        by_experiment = await store.list_summaries(experiment_name="Tiny Demo")
        self.assertEqual([item["operation_id"] for item in by_experiment], ["op_a"])
        # Exact match: no substring or case-insensitive matching.
        self.assertEqual(await store.list_summaries(experiment_name="tiny demo"), [])
        self.assertEqual(await store.list_summaries(experiment_name="Tiny"), [])
        by_run = await store.list_summaries(run_id=other_run)
        self.assertEqual([item["operation_id"] for item in by_run], ["op_b"])

        everything = {item["operation_id"]: item for item in await store.list_summaries()}
        self.assertEqual(set(everything), {"op_a", "op_b", "op_c"})
        # Operations recorded without these fields report null.
        self.assertIsNone(everything["op_c"]["run_id"])
        self.assertIsNone(everything["op_c"]["model_uri"])
        self.assertIsNone(everything["op_c"]["experiment_name"])


class DeployOrchestratorTest(unittest.IsolatedAsyncioTestCase):
    async def test_run_operation_ready(self) -> None:
        store = DeployOperationStore(now_iso=lambda: "2026-04-15T11:00:00+00:00")
        request = _build_request()
        operation = _build_operation("op_ready", request)
        await store.add(operation)

        fake_k8s = _FakeKubernetesApiClient()
        builder_calls: dict[str, dict] = {}

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
            build_delete_job_manifest=lambda **kwargs: {"metadata": {"name": kwargs["job_name"]}},
            build_deployment_manifest=lambda **kwargs: builder_calls.setdefault(
                "deployment", kwargs
            ),
            build_service_manifest=lambda **kwargs: builder_calls.setdefault("service", kwargs),
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
        # The container loads the model baked into the image; the MLflow URI goes to metadata.
        self.assertEqual(builder_calls["deployment"]["model_uri"], "/models/model")
        for kind in ("deployment", "service"):
            self.assertEqual(builder_calls[kind]["run_id"], RUN_ID)
            self.assertEqual(
                builder_calls[kind]["source_model_uri"], f"runs:/{RUN_ID}/ludwig-model"
            )
            self.assertEqual(builder_calls[kind]["experiment_name"], "Tiny Demo")

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
            build_delete_job_manifest=lambda **kwargs: {"metadata": {"name": kwargs["job_name"]}},
            build_deployment_manifest=lambda **kwargs: {
                "metadata": {"name": f"model-{kwargs['model_slug']}"}
            },
            build_service_manifest=lambda **kwargs: {
                "metadata": {"name": f"model-{kwargs['model_slug']}"}
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
        self.assertEqual(result["error_log"], "boom")

    async def test_run_operation_build_failure_splits_reason_and_log(self) -> None:
        store = DeployOperationStore(now_iso=lambda: "2026-04-15T11:05:00+00:00")
        request = _build_request(job_name="build-model-push-abc123")
        await store.add(_build_operation("op_push", request))

        class _LogsK8s(_FakeKubernetesApiClient):
            async def read_job_logs(self, client, base_url, headers, namespace, job_name, container):
                if container == "kaniko":
                    return (
                        "\x1b[36mINFO\x1b[0m[0000] Retrieving image manifest\n"
                        "error checking push permissions: UNAUTHORIZED: "
                        "unauthorized to access repository\n"
                    )
                return "Model downloaded.\n"

        def handler(request_obj: httpx.Request) -> httpx.Response:
            if request_obj.method == "POST":
                return httpx.Response(status_code=201, json={"kind": "Job"})
            return httpx.Response(status_code=200, json={"status": {"failed": 1}})

        transport = httpx.MockTransport(handler)
        orchestrator = DeployOrchestrator(
            operation_store=store,
            k8s_client=_LogsK8s(),  # type: ignore[arg-type]
            resolve_k8s_auth=lambda: ("https://kubernetes.default.svc", {}, False),
            build_job_manifest=lambda **kwargs: {"metadata": {"name": kwargs["job_name"]}},
            build_delete_job_manifest=lambda **kwargs: {},
            build_deployment_manifest=lambda **kwargs: {},
            build_service_manifest=lambda **kwargs: {},
            kaniko_image="kaniko",
            model_service_prefix="model",
            build_timeout_sec=10,
            rollout_timeout_sec=10,
            http_client_factory=lambda verify: httpx.AsyncClient(transport=transport, verify=False),
        )

        await orchestrator.run_operation("op_push")
        result = await store.get("op_push")
        assert result is not None
        self.assertEqual(result["status"], "failed")
        self.assertIn("Registry push denied", result["error"] or "")
        self.assertIn("model-registry-creds", result["error"] or "")
        self.assertNotIn("Retrieving image manifest", result["error"] or "")
        self.assertIn("Retrieving image manifest", result["error_log"] or "")
        self.assertIn("[prepare-build-context]\nModel downloaded.", result["error_log"] or "")

        summaries = await store.list_summaries()
        self.assertNotIn("error_log", summaries[0])


class GatewayDeployApiTest(unittest.IsolatedAsyncioTestCase):
    async def test_deploy_model_and_deploy_status_contract(self) -> None:
        service = GatewayService()
        service.model_base_image = "registry.example.com/api-models/model-service-base:py310"
        service.deploy_handler.model_base_image = service.model_base_image
        service.deploy_orchestrator.run_operation = AsyncMock(return_value=None)
        service.deploy_orchestrator.find_model_resource = AsyncMock(return_value=None)

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
        service.deploy_orchestrator.find_model_resource = AsyncMock(return_value=None)

        response = await service.deploy_model(
            model_name="tiny_demo_model",
            model_version="latest",
            tracking_uri="http://mlflow:5000",
            target_image="registry.example.com/api-models/model-tiny-demo:latest",
            deployed_name="tiny_alias",
            append_date=True,
        )

        self.assertRegex(response["deployed_name"], r"^tiny_alias_[0-9]{8}$")
        self.assertRegex(response["k8s_job"], r"^bentloy-build-tiny-alias-[0-9]{8}-[a-z0-9]{6}$")

        operation_id = response["operation_id"]
        status = await service.deploy_status(operation_id)
        self.assertRegex(status["deployed_name"], r"^tiny_alias_[0-9]{8}$")

    async def test_deploy_model_stores_run_and_experiment(self) -> None:
        service = GatewayService()
        service.model_base_image = "registry.example.com/api-models/model-service-base:py310"
        service.deploy_handler.model_base_image = service.model_base_image
        service.deploy_orchestrator.run_operation = AsyncMock(return_value=None)
        service.deploy_orchestrator.find_model_resource = AsyncMock(return_value=None)
        service.info_handler.discover_k8s_model_services = AsyncMock(return_value=([], None))

        model_uri = f"runs:/{RUN_ID}/ludwig-model"
        response = await service.deploy_model(
            model_name="churn_v2",
            run_id=RUN_ID,
            model_uri=model_uri,
            experiment_name="Churn prediction",
            target_image="registry.example.com/api-models/model-churn:latest",
        )
        self.assertEqual(response["run_id"], RUN_ID)
        self.assertEqual(response["model_uri"], model_uri)
        self.assertEqual(response["experiment_name"], "Churn prediction")

        operation = await service.operation_store.get(response["operation_id"])
        assert operation is not None
        for record in (operation, operation["request"]):
            self.assertEqual(record["run_id"], RUN_ID)
            self.assertEqual(record["model_uri"], model_uri)
            self.assertEqual(record["experiment_name"], "Churn prediction")

        status = await service.deploy_status(response["operation_id"])
        self.assertEqual(status["run_id"], RUN_ID)
        self.assertEqual(status["model_uri"], model_uri)
        self.assertEqual(status["experiment_name"], "Churn prediction")

        listing = await service.list_models(experiment_name="Churn prediction")
        self.assertEqual(
            [op["operation_id"] for op in listing["deploy_operations"]],
            [response["operation_id"]],
        )
        self.assertEqual(listing["deploy_operations"][0]["run_id"], RUN_ID)
        self.assertEqual(listing["deploy_operations"][0]["model_uri"], model_uri)
        self.assertEqual(
            (await service.list_models(experiment_name="Other"))["deploy_operations"], []
        )


class _FakeResponse:
    def __init__(self) -> None:
        self.status_code = 200


class _FakeContext:
    def __init__(self) -> None:
        self.response = _FakeResponse()


class GatewayDeployErrorsTest(unittest.IsolatedAsyncioTestCase):
    def _service(self) -> GatewayService:
        service = GatewayService()
        service.deploy_orchestrator.run_operation = AsyncMock(return_value=None)
        service.deploy_orchestrator.find_model_resource = AsyncMock(return_value=None)
        service.deploy_handler.model_base_image = "registry.example.com/api-models/base:py312"
        return service

    async def _deploy(self, service: GatewayService, **kwargs) -> tuple[int, dict]:
        ctx = _FakeContext()
        params = {
            "model_name": "tiny_demo_model",
            "tracking_uri": "http://mlflow:5000",
            "target_image": "registry.example.com/api-models/model-tiny-demo:latest",
            **kwargs,
        }
        result = await service.deploy_model(ctx=ctx, **params)
        return ctx.response.status_code, result

    async def test_validation_errors_answer_400_with_detail(self) -> None:
        service = self._service()
        cases = {
            "replicas must be >= 1.": {"replicas": 0},
            "Invalid model_name": {"model_name": "!!!"},
            "Invalid namespace": {"namespace": "Bad_NS"},
            "Invalid run_id": {"run_id": "a" * 64},
            "Invalid target_image": {"target_image": "UPPER/CASE"},
        }
        for expected, kwargs in cases.items():
            with self.subTest(expected=expected):
                status_code, body = await self._deploy(service, **kwargs)
                self.assertEqual(status_code, 400)
                self.assertIn(expected, body["detail"])
                self.assertEqual(body["error"], body["detail"])
                self.assertNotIn("operation_id", body)

    async def test_run_id_must_be_a_label_value(self) -> None:
        service = self._service()
        for bad in ("-abcdef", "abcdef.", "ab c1", "abc"):
            status_code, _ = await self._deploy(service, run_id=bad)
            self.assertEqual(status_code, 400, bad)
        status_code, body = await self._deploy(service, run_id=RUN_ID)
        self.assertEqual(status_code, 200)
        self.assertIn("operation_id", body)

    async def test_deployed_name_too_long_for_kubernetes(self) -> None:
        service = self._service()
        # bentloy-model- (14 chars) + 49-char slug = 63: accepted.
        status_code, body = await self._deploy(service, deployed_name="a" * 49)
        self.assertEqual(status_code, 200, body)
        self.assertLessEqual(len(body["k8s_job"]), 63)
        self.assertTrue(body["k8s_job"].startswith("bentloy-build-"))
        status_code, body = await self._deploy(service, deployed_name="a" * 50)
        self.assertEqual(status_code, 400)
        self.assertIn("deployed_name too long", body["detail"])

    async def test_disabled_deploy_answers_403(self) -> None:
        service = self._service()
        service.deploy_handler.auto_deploy_enabled = False
        status_code, body = await self._deploy(service)
        self.assertEqual(status_code, 403)
        self.assertIn("disabled", body["detail"])

    async def test_list_models_page_is_one_indexed(self) -> None:
        service = self._service()
        service.info_handler.discover_k8s_model_services = AsyncMock(return_value=([], None))

        ctx = _FakeContext()
        result = await service.list_models(ctx=ctx)
        self.assertEqual(ctx.response.status_code, 200)
        self.assertEqual(result["pagination"]["endpoints"]["current_page"], 1)

        for page in (0, -1):
            ctx = _FakeContext()
            body = await service.list_models(page=page, ctx=ctx)
            self.assertEqual(ctx.response.status_code, 400, page)
            self.assertIn("page must be >= 1", body["detail"])
            self.assertEqual(body["error"], body["detail"])
            self.assertNotIn("pagination", body)

    async def test_default_images_with_and_without_namespace(self) -> None:
        cases = {
            "api-models": (
                "reg.example.com/api-models/model-tiny-demo-model:",
                "reg.example.com/api-models/model-service-base:py312",
            ),
            "": ("reg.example.com/model-tiny-demo-model:", "reg.example.com/model-service-base:py312"),
        }
        for namespace, (target_prefix, base_image) in cases.items():
            with self.subTest(namespace=namespace):
                service = self._service()
                handler = service.deploy_handler
                handler.model_base_image = ""
                handler.registry_server = "reg.example.com"
                handler.registry_namespace = namespace
                ctx = _FakeContext()
                body = await service.deploy_model(
                    ctx=ctx, model_name="tiny_demo_model", tracking_uri="http://mlflow:5000"
                )
                self.assertEqual(ctx.response.status_code, 200, body)
                self.assertTrue(body["image_ref"].startswith(target_prefix), body["image_ref"])
                operation = await service.operation_store.get(body["operation_id"])
                self.assertEqual(operation["request"]["base_image"], base_image)

    async def test_gateway_misconfiguration_answers_500(self) -> None:
        service = self._service()
        service.deploy_handler.model_base_image = ""
        service.deploy_handler.registry_server = ""
        service.deploy_handler.registry_namespace = ""
        status_code, body = await self._deploy(service)
        self.assertEqual(status_code, 500)
        self.assertIn("MODEL_BASE_IMAGE", body["detail"])


def _existing_service(run_id: str = RUN_ID, experiment_name: str = "Churn Prediction") -> dict:
    """Metadata of an existing model-service Service, as the Kubernetes API returns it."""

    return {
        "name": "bentloy-model-churn-v2",
        "labels": {
            "app": "model-service",
            "model-slug": "churn-v2",
            "bentloy.io/run-id": run_id,
            "bentloy.io/experiment": "churn-prediction",
        },
        "annotations": {"bentloy.io/experiment-name": experiment_name},
    }


class GatewayDeployConflictTest(unittest.IsolatedAsyncioTestCase):
    def _service(self, existing: dict | None = None) -> GatewayService:
        service = GatewayService()
        service.deploy_orchestrator.run_operation = AsyncMock(return_value=None)
        service.deploy_orchestrator.find_model_resource = AsyncMock(return_value=existing)
        service.deploy_handler.model_base_image = "registry.example.com/api-models/base:py312"
        return service

    async def _deploy(self, service: GatewayService, **kwargs) -> tuple[int, dict]:
        ctx = _FakeContext()
        params = {
            "model_name": "churn_model",
            "tracking_uri": "http://mlflow:5000",
            "target_image": "registry.example.com/api-models/model-churn:latest",
            **kwargs,
        }
        result = await service.deploy_model(ctx=ctx, **params)
        return ctx.response.status_code, result

    async def test_new_name_deploys_as_before(self) -> None:
        service = self._service()
        status_code, body = await self._deploy(service, deployed_name="Churn V2")
        self.assertEqual(status_code, 200, body)
        self.assertIn("operation_id", body)
        service.deploy_orchestrator.find_model_resource.assert_awaited_once_with(
            service.deploy_handler.model_namespace, "bentloy-model-churn-v2"
        )

    async def test_existing_service_answers_409_with_run_and_experiment(self) -> None:
        service = self._service(existing=_existing_service())
        status_code, body = await self._deploy(service, deployed_name="Churn V2")
        self.assertEqual(status_code, 409)
        self.assertEqual(body["error"], body["detail"])
        for expected in ("'Churn V2'", "bentloy-model-churn-v2", RUN_ID, "'Churn Prediction'",
                         "overwrite=true"):
            self.assertIn(expected, body["detail"])
        self.assertNotIn("operation_id", body)
        self.assertEqual(await service.operation_store.list_summaries(), [])

    async def test_existing_resource_without_metadata_answers_409(self) -> None:
        service = self._service(existing={"name": "bentloy-model-churn-v2"})
        status_code, body = await self._deploy(service, deployed_name="churn-v2")
        self.assertEqual(status_code, 409)
        self.assertIn("an existing deployment", body["detail"])

    async def test_names_with_the_same_slug_conflict(self) -> None:
        service = self._service()
        status_code, body = await self._deploy(service, deployed_name="Churn_V2")
        self.assertEqual(status_code, 200, body)
        # The first deploy has finished: its resources now exist in Kubernetes.
        await service.operation_store.update(body["operation_id"], status="ready")
        service.deploy_orchestrator.find_model_resource.return_value = _existing_service()

        status_code, body = await self._deploy(service, deployed_name="churn v2")
        self.assertEqual(status_code, 409)
        self.assertIn("'churn v2' maps to bentloy-model-churn-v2", body["detail"])

        status_code, body = await self._deploy(service, deployed_name="churn v2", overwrite=True)
        self.assertEqual(status_code, 200, body)
        self.assertEqual(body["deployed_name"], "churn v2")

    async def test_overwrite_skips_the_existence_check(self) -> None:
        service = self._service(existing=_existing_service())
        status_code, body = await self._deploy(service, deployed_name="Churn_V2", overwrite=True)
        self.assertEqual(status_code, 200, body)
        service.deploy_orchestrator.find_model_resource.assert_not_awaited()

    async def test_operation_in_progress_answers_409_even_with_overwrite(self) -> None:
        service = self._service()
        status_code, first = await self._deploy(
            service, deployed_name="Churn_V2", run_id=RUN_ID, experiment_name="Churn Prediction"
        )
        self.assertEqual(status_code, 200, first)

        for status in ("queued", "building", "pushing", "deploying", "deleting"):
            await service.operation_store.update(first["operation_id"], status=status)
            for overwrite in (False, True):
                with self.subTest(status=status, overwrite=overwrite):
                    status_code, body = await self._deploy(
                        service, deployed_name="churn v2", overwrite=overwrite
                    )
                    self.assertEqual(status_code, 409)
                    for expected in (first["operation_id"], f"'{status}'", RUN_ID,
                                     "'Churn Prediction'", "in progress"):
                        self.assertIn(expected, body["detail"])

        # Other namespaces and finished operations do not block.
        status_code, body = await self._deploy(
            service, deployed_name="churn v2", namespace="other-ns"
        )
        self.assertEqual(status_code, 200, body)
        await service.operation_store.update(first["operation_id"], status="failed")
        status_code, body = await self._deploy(service, deployed_name="churn v2")
        self.assertEqual(status_code, 200, body)

    async def test_concurrent_requests_queue_only_one(self) -> None:
        service = self._service()
        results = await asyncio.gather(
            self._deploy(service, deployed_name="Churn_V2"),
            self._deploy(service, deployed_name="churn v2"),
        )
        self.assertEqual(sorted(code for code, _ in results), [200, 409])
        self.assertEqual(len(await service.operation_store.list_summaries()), 1)

    async def test_kubernetes_unreadable_answers_503(self) -> None:
        service = self._service()
        service.deploy_orchestrator.find_model_resource = AsyncMock(
            side_effect=RuntimeError("Read failed (403): forbidden")
        )
        status_code, body = await self._deploy(service, deployed_name="Churn V2")
        self.assertEqual(status_code, 503)
        self.assertIn("forbidden", body["detail"])
        self.assertEqual(await service.operation_store.list_summaries(), [])

    async def test_empty_or_too_long_slug_answers_400(self) -> None:
        service = self._service(existing=_existing_service())
        cases = {
            "Invalid deployed_name": {"deployed_name": "!!! ___"},
            "deployed_name too long": {"deployed_name": "a" * 50},
        }
        for expected, kwargs in cases.items():
            with self.subTest(expected=expected):
                status_code, body = await self._deploy(service, overwrite=True, **kwargs)
                self.assertEqual(status_code, 400)
                self.assertIn(expected, body["detail"])
                self.assertEqual(body["error"], body["detail"])
        service.deploy_orchestrator.find_model_resource.assert_not_awaited()


class FindModelResourceTest(unittest.IsolatedAsyncioTestCase):
    def _orchestrator(self, responses: dict) -> tuple[DeployOrchestrator, list]:
        requested: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            requested.append(request.url.path)
            return responses.get(request.url.path, httpx.Response(404))

        orchestrator = DeployOrchestrator(
            operation_store=DeployOperationStore(),
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
                transport=httpx.MockTransport(handler)
            ),
        )
        return orchestrator, requested

    async def test_service_then_deployment(self) -> None:
        service_path = "/api/v1/namespaces/ns/services/bentloy-model-x"
        deployment_path = "/apis/apps/v1/namespaces/ns/deployments/bentloy-model-x"
        metadata = {"name": "bentloy-model-x", "labels": {"bentloy.io/run-id": RUN_ID}}

        orchestrator, requested = self._orchestrator({})
        self.assertIsNone(await orchestrator.find_model_resource("ns", "bentloy-model-x"))
        self.assertEqual(requested, [service_path, deployment_path])

        orchestrator, _ = self._orchestrator(
            {deployment_path: httpx.Response(200, json={"metadata": metadata})}
        )
        self.assertEqual(await orchestrator.find_model_resource("ns", "bentloy-model-x"), metadata)

        orchestrator, _ = self._orchestrator({service_path: httpx.Response(403, text="forbidden")})
        with self.assertRaisesRegex(RuntimeError, "403"):
            await orchestrator.find_model_resource("ns", "bentloy-model-x")


if __name__ == "__main__":
    unittest.main()
