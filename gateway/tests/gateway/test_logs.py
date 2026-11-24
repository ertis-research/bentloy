import unittest

import httpx

from gateway.app.deploy.operation_store import DeployOperationStore
from gateway.app.handlers.logs_handler import LogsHandler, LogsRequestError
from gateway.app.kubernetes.client import KubernetesApiClient
from gateway.service import GatewayService

NS = "api-models"
PREFIX = "bentloy-model"
SERVICE = f"{PREFIX}-churn-v2"
JOB = "bentloy-build-churn-v2-abc123"
DEPLOYMENT_URL = f"/apis/apps/v1/namespaces/{NS}/deployments/{SERVICE}"
JOB_URL = f"/apis/batch/v1/namespaces/{NS}/jobs/{JOB}"
PODS_URL = f"/api/v1/namespaces/{NS}/pods"


def _pod(name: str, created: str, statuses=None, init_statuses=None, deleting=False) -> dict:
    metadata = {"name": name, "creationTimestamp": created}
    if deleting:
        metadata["deletionTimestamp"] = "2026-01-01T00:00:00Z"
    status: dict = {"phase": "Running"}
    if statuses is not None:
        status["containerStatuses"] = statuses
    if init_statuses is not None:
        status["initContainerStatuses"] = init_statuses
    return {"metadata": metadata, "status": status}


class _Cluster:
    """Fake Kubernetes API recording the requests it receives."""

    def __init__(self) -> None:
        self.deployment: dict | None = {"spec": {"replicas": 1}}
        self.job: dict | None = {"metadata": {"name": JOB}}
        self.pods: list[dict] = []
        self.pods_status = 200
        self.logs: dict[str, tuple[int, str]] = {}  # container -> (status, text)
        self.requests: list[httpx.Request] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        path = request.url.path
        if path == DEPLOYMENT_URL:
            return httpx.Response(404) if self.deployment is None else httpx.Response(
                200, json=self.deployment
            )
        if path == JOB_URL:
            return httpx.Response(404) if self.job is None else httpx.Response(200, json=self.job)
        if path == PODS_URL:
            return httpx.Response(self.pods_status, json={"items": self.pods})
        if path.endswith("/log"):
            status, text = self.logs.get(request.url.params["container"], (404, ""))
            return httpx.Response(status, text=text)
        return httpx.Response(404)

    def log_requests(self) -> list[httpx.Request]:
        return [r for r in self.requests if r.url.path.endswith("/log")]


def _handler(cluster: _Cluster, store: DeployOperationStore | None = None) -> LogsHandler:
    return LogsHandler(
        resolve_k8s_auth=lambda: ("https://k8s", {}, False),
        k8s_client=KubernetesApiClient(),
        operation_store=store or DeployOperationStore(),
        model_namespace=NS,
        model_service_prefix=PREFIX,
        http_client_factory=lambda verify: httpx.AsyncClient(
            transport=httpx.MockTransport(cluster.handler)
        ),
    )


def _model_status(ready=True, restarts=0, waiting=None) -> dict:
    state = {"waiting": {"reason": waiting}} if waiting else {"running": {}}
    return {"name": "model-service", "ready": ready, "restartCount": restarts, "state": state}


class DeploymentLogsTest(unittest.IsolatedAsyncioTestCase):
    async def test_happy_path_picks_newest_non_terminating_pod(self) -> None:
        cluster = _Cluster()
        cluster.pods = [
            _pod("old", "2026-01-01T00:00:00Z", [_model_status()]),
            _pod("terminating", "2026-01-03T00:00:00Z", [_model_status()], deleting=True),
            _pod("new", "2026-01-02T00:00:00Z", [_model_status(True, 2)]),
        ]
        cluster.logs["model-service"] = (200, "hello\n")

        result = await _handler(cluster).deployment_logs(SERVICE, tail_lines=100)

        self.assertEqual(
            result,
            {
                "service_name": SERVICE,
                "pod_name": "new",
                "pod_phase": "Running",
                "container_ready": True,
                "restart_count": 2,
                "available": True,
                "reason": None,
                "logs": "hello\n",
            },
        )
        pods_request = next(r for r in cluster.requests if r.url.path == PODS_URL)
        self.assertEqual(
            pods_request.url.params["labelSelector"], "app=model-service,model-slug=churn-v2"
        )
        (log_request,) = cluster.log_requests()
        self.assertEqual(log_request.url.path, f"{PODS_URL}/new/log")
        params = log_request.url.params
        self.assertEqual(params["container"], "model-service")
        self.assertEqual(params["tailLines"], "100")
        self.assertEqual(params["timestamps"], "false")
        self.assertNotIn("previous", params)

    async def test_previous_param_only_when_set(self) -> None:
        cluster = _Cluster()
        cluster.pods = [_pod("p", "2026-01-01T00:00:00Z", [_model_status(True, 1)])]
        cluster.logs["model-service"] = (200, "old run")

        result = await _handler(cluster).deployment_logs(SERVICE, previous=True)

        self.assertTrue(result["available"])
        self.assertEqual(cluster.log_requests()[0].url.params["previous"], "true")

    async def test_unknown_or_invalid_service_name_is_404(self) -> None:
        cluster = _Cluster()
        for name in ("gateway", PREFIX, f"{PREFIX}-", "other-churn"):
            with self.assertRaises(LogsRequestError) as ctx:
                await _handler(cluster).deployment_logs(name)
            self.assertEqual(ctx.exception.status_code, 404, name)
        self.assertEqual(cluster.requests, [])

    async def test_missing_deployment_is_404(self) -> None:
        cluster = _Cluster()
        cluster.deployment = None
        with self.assertRaises(LogsRequestError) as ctx:
            await _handler(cluster).deployment_logs(SERVICE)
        self.assertEqual(ctx.exception.status_code, 404)
        self.assertIn(SERVICE, ctx.exception.message)

    async def test_scaled_to_zero(self) -> None:
        cluster = _Cluster()
        cluster.deployment = {"spec": {"replicas": 0}}
        result = await _handler(cluster).deployment_logs(SERVICE)
        self.assertFalse(result["available"])
        self.assertEqual(result["reason"], "The deployment is scaled to 0 replicas.")
        self.assertIsNone(result["pod_name"])
        self.assertEqual(result["logs"], "")
        self.assertEqual(result["restart_count"], 0)

    async def test_no_pods(self) -> None:
        cluster = _Cluster()
        cluster.pods = [_pod("gone", "2026-01-01T00:00:00Z", deleting=True)]
        result = await _handler(cluster).deployment_logs(SERVICE)
        self.assertFalse(result["available"])
        self.assertEqual(result["reason"], "No running pod for this model yet.")
        self.assertIsNone(result["pod_name"])

    async def test_container_not_started_reports_starting_with_waiting_reason(self) -> None:
        cluster = _Cluster()
        cluster.pods = [
            _pod("p", "2026-01-01T00:00:00Z", [_model_status(False, 0, "ContainerCreating")])
        ]
        cluster.logs["model-service"] = (400, "container is waiting to start")
        result = await _handler(cluster).deployment_logs(SERVICE)
        self.assertFalse(result["available"])
        self.assertEqual(result["reason"], "The model pod is starting. (ContainerCreating)")
        self.assertEqual(result["pod_name"], "p")
        self.assertFalse(result["container_ready"])
        self.assertEqual(result["logs"], "")

    async def test_previous_without_restart(self) -> None:
        cluster = _Cluster()
        cluster.pods = [_pod("p", "2026-01-01T00:00:00Z", [_model_status()])]
        cluster.logs["model-service"] = (400, "previous terminated container not found")
        result = await _handler(cluster).deployment_logs(SERVICE, previous=True)
        self.assertFalse(result["available"])
        self.assertIn("No previous container", result["reason"])

    async def test_previous_after_restart_without_logs(self) -> None:
        cluster = _Cluster()
        cluster.pods = [_pod("p", "2026-01-01T00:00:00Z", [_model_status(True, 3)])]
        cluster.logs["model-service"] = (400, "previous terminated container not found")
        result = await _handler(cluster).deployment_logs(SERVICE, previous=True)
        self.assertFalse(result["available"])
        self.assertEqual(result["reason"], "The previous container's logs are not available.")

    async def test_pod_log_404(self) -> None:
        cluster = _Cluster()
        cluster.pods = [_pod("p", "2026-01-01T00:00:00Z", [_model_status()])]
        result = await _handler(cluster).deployment_logs(SERVICE)
        self.assertFalse(result["available"])
        self.assertEqual(result["reason"], "The model pod no longer exists.")

    async def test_kubernetes_errors_are_502(self) -> None:
        cluster = _Cluster()
        cluster.pods = [_pod("p", "2026-01-01T00:00:00Z", [_model_status()])]
        cluster.logs["model-service"] = (500, "boom")
        with self.assertRaises(LogsRequestError) as ctx:
            await _handler(cluster).deployment_logs(SERVICE)
        self.assertEqual(ctx.exception.status_code, 502)
        self.assertIn("500", ctx.exception.message)

        cluster.pods_status = 403
        with self.assertRaises(LogsRequestError) as ctx:
            await _handler(cluster).deployment_logs(SERVICE)
        self.assertEqual(ctx.exception.status_code, 502)

    async def test_transport_error_and_auth_failure_are_502(self) -> None:
        def failing(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("down")

        handler = _handler(_Cluster())
        handler.http_client_factory = lambda verify: httpx.AsyncClient(
            transport=httpx.MockTransport(failing)
        )
        with self.assertRaises(LogsRequestError) as ctx:
            await handler.deployment_logs(SERVICE)
        self.assertEqual(ctx.exception.status_code, 502)

        def no_auth():
            raise RuntimeError("not in cluster")

        handler = _handler(_Cluster())
        handler.resolve_k8s_auth = no_auth
        with self.assertRaises(LogsRequestError) as ctx:
            await handler.deployment_logs(SERVICE)
        self.assertEqual(ctx.exception.status_code, 502)

    async def test_tail_lines_out_of_range_is_400(self) -> None:
        cluster = _Cluster()
        for value in (0, 5001, -1):
            with self.assertRaises(LogsRequestError) as ctx:
                await _handler(cluster).deployment_logs(SERVICE, tail_lines=value)
            self.assertEqual(ctx.exception.status_code, 400)
            self.assertEqual(ctx.exception.message, "tail_lines must be between 1 and 5000.")
        for value in (1, 5000):
            await _handler(cluster).deployment_logs(SERVICE, tail_lines=value)


class BuildLogsTest(unittest.IsolatedAsyncioTestCase):
    async def _store(self, **changes) -> DeployOperationStore:
        store = DeployOperationStore()
        operation = {
            "operation_id": "op_1",
            "status": "building",
            "step": "kaniko_push",
            "namespace": NS,
            "k8s_job": JOB,
            "request": {"namespace": NS, "deployment_slug": "churn-v2"},
        }
        operation.update(changes)
        await store.add(operation)
        return store

    def _build_pod(self, init_statuses=None, statuses=None) -> dict:
        return _pod("build-pod", "2026-01-01T00:00:00Z", statuses, init_statuses)

    async def test_unknown_operation_is_404(self) -> None:
        with self.assertRaises(LogsRequestError) as ctx:
            await _handler(_Cluster()).build_logs("nope")
        self.assertEqual(ctx.exception.status_code, 404)
        self.assertIn("nope", ctx.exception.message)

    async def test_queued_operation_has_no_job_yet(self) -> None:
        cluster = _Cluster()
        store = await self._store(step="queued", status="queued")
        result = await _handler(cluster, store).build_logs("op_1")
        self.assertFalse(result["available"])
        self.assertEqual(result["reason"], "The build job has not been created yet.")
        self.assertEqual(result["step"], "queued")
        self.assertEqual(cluster.requests, [])

    async def test_job_removed_after_ttl(self) -> None:
        cluster = _Cluster()
        cluster.job = None
        result = await _handler(cluster, await self._store()).build_logs("op_1")
        self.assertFalse(result["available"])
        self.assertIn("no longer exists", result["reason"])
        self.assertEqual(result["job_name"], JOB)

    async def test_job_404_during_create_job_is_not_ttl(self) -> None:
        cluster = _Cluster()
        cluster.job = None
        store = await self._store(step="create_job")
        result = await _handler(cluster, store).build_logs("op_1")
        self.assertFalse(result["available"])
        self.assertEqual(result["reason"], "The build job has not been created yet.")

    async def test_no_pod_yet(self) -> None:
        result = await _handler(_Cluster(), await self._store()).build_logs("op_1")
        self.assertFalse(result["available"])
        self.assertEqual(result["reason"], "The build pod has not been created yet.")

    async def test_both_containers_formatted_with_headers(self) -> None:
        cluster = _Cluster()
        cluster.pods = [self._build_pod()]
        cluster.logs = {
            "prepare-build-context": (200, "prepared\n"),
            "kaniko": (200, "pushed"),
        }
        result = await _handler(cluster, await self._store()).build_logs("op_1", tail_lines=50)

        self.assertTrue(result["available"])
        self.assertIsNone(result["reason"])
        self.assertEqual(result["pod_name"], "build-pod")
        self.assertEqual(result["pod_phase"], "Running")
        self.assertEqual(result["step"], "kaniko_push")
        self.assertEqual(
            result["logs"],
            "===== prepare-build-context =====\nprepared\n\n===== kaniko =====\npushed\n",
        )
        containers = [r.url.params["container"] for r in cluster.log_requests()]
        self.assertEqual(containers, ["prepare-build-context", "kaniko"])
        for request in cluster.log_requests():
            self.assertEqual(request.url.params["tailLines"], "50")
            self.assertNotIn("previous", request.url.params)

    async def test_init_done_and_kaniko_not_started(self) -> None:
        cluster = _Cluster()
        cluster.pods = [
            self._build_pod(
                statuses=[{"name": "kaniko", "state": {"waiting": {"reason": "PodInitializing"}}}]
            )
        ]
        cluster.logs = {
            "prepare-build-context": (200, "prepared\n"),
            "kaniko": (400, "waiting"),
        }
        result = await _handler(cluster, await self._store()).build_logs("op_1")
        self.assertTrue(result["available"])
        self.assertEqual(
            result["logs"],
            "===== prepare-build-context =====\nprepared\n\n"
            "===== kaniko =====\n(not started yet: PodInitializing)\n",
        )

    async def test_nothing_started_is_unavailable(self) -> None:
        cluster = _Cluster()
        cluster.pods = [
            self._build_pod(
                init_statuses=[
                    {
                        "name": "prepare-build-context",
                        "state": {"waiting": {"reason": "PodInitializing"}},
                    }
                ]
            )
        ]
        cluster.logs = {
            "prepare-build-context": (400, "waiting"),
            "kaniko": (400, "waiting"),
        }
        result = await _handler(cluster, await self._store()).build_logs("op_1")
        self.assertFalse(result["available"])
        self.assertEqual(result["reason"], "The build pod is starting. (PodInitializing)")
        self.assertEqual(result["logs"], "")

    async def test_container_log_404_marks_section(self) -> None:
        cluster = _Cluster()
        cluster.pods = [self._build_pod()]
        cluster.logs = {"kaniko": (200, "ok\n")}
        result = await _handler(cluster, await self._store()).build_logs("op_1")
        self.assertTrue(result["available"])
        self.assertIn("===== prepare-build-context =====\n(logs not available)\n", result["logs"])

    async def test_kubernetes_errors_are_502(self) -> None:
        cluster = _Cluster()
        cluster.pods = [self._build_pod()]
        cluster.logs = {"prepare-build-context": (500, "boom")}
        with self.assertRaises(LogsRequestError) as ctx:
            await _handler(cluster, await self._store()).build_logs("op_1")
        self.assertEqual(ctx.exception.status_code, 502)

    async def test_tail_lines_out_of_range_is_400(self) -> None:
        for value in (0, 5001):
            with self.assertRaises(LogsRequestError) as ctx:
                await _handler(_Cluster(), await self._store()).build_logs("op_1", value)
            self.assertEqual(ctx.exception.status_code, 400)


class _FakeResponse:
    def __init__(self) -> None:
        self.status_code = 200


class _FakeContext:
    def __init__(self) -> None:
        self.response = _FakeResponse()


class GatewayLogsEndpointsTest(unittest.IsolatedAsyncioTestCase):
    async def test_errors_set_status_code_and_body(self) -> None:
        service = GatewayService()

        ctx = _FakeContext()
        body = await service.build_logs("missing", ctx=ctx)
        self.assertEqual(ctx.response.status_code, 404)
        self.assertEqual(body["error"], body["detail"])
        self.assertIn("missing", body["detail"])

        ctx = _FakeContext()
        body = await service.deployment_logs("bentloy-model-x", tail_lines=0, ctx=ctx)
        self.assertEqual(ctx.response.status_code, 400)
        self.assertEqual(body["detail"], "tail_lines must be between 1 and 5000.")

        ctx = _FakeContext()
        body = await service.deployment_logs("unrelated", ctx=ctx)
        self.assertEqual(ctx.response.status_code, 404)

    async def test_success_keeps_status_200(self) -> None:
        cluster = _Cluster()
        cluster.deployment = {"spec": {"replicas": 0}}
        service = GatewayService()
        service.logs_handler = _handler(cluster)
        ctx = _FakeContext()
        body = await service.deployment_logs(SERVICE, ctx=ctx)
        self.assertEqual(ctx.response.status_code, 200)
        self.assertFalse(body["available"])


class PodLookupCompatTest(unittest.IsolatedAsyncioTestCase):
    async def test_get_job_pod_name_and_read_job_logs_unchanged(self) -> None:
        cluster = _Cluster()
        cluster.pods = [
            _pod("a", "2026-01-01T00:00:00Z"),
            _pod("b", "2026-01-02T00:00:00Z"),
        ]
        cluster.logs["kaniko"] = (200, "text")
        client = KubernetesApiClient()
        async with httpx.AsyncClient(transport=httpx.MockTransport(cluster.handler)) as http:
            name = await client.get_job_pod_name(http, "https://k8s", {}, NS, JOB)
            logs = await client.read_job_logs(http, "https://k8s", {}, NS, JOB, "kaniko")
            cluster.pods_status = 500
            failed = await client.get_job_pod_name(http, "https://k8s", {}, NS, JOB)
        self.assertEqual(name, "b")
        self.assertEqual(logs, "text")
        self.assertIsNone(failed)


if __name__ == "__main__":
    unittest.main()
