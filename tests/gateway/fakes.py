"""Fake collaborators shared by the gateway tests.

The Kubernetes API is simulated with ``httpx.MockTransport`` (see ``FakeCluster``); the other
fakes replace the HTTP clients and BentoML request context used by the handlers.
"""

from collections.abc import Callable
from typing import Any

import httpx

from gateway.app.domain.types import DeployOperationRecord, DeployOperationRequest
from gateway.app.routing.discovery import KubernetesModelDiscovery

NS = "api-models"
RUN_ID = "0123456789abcdef0123456789abcdef"
PREFIX = "bentloy-model"
DEPLOYMENTS = f"/apis/apps/v1/namespaces/{NS}/deployments"
SERVICES = f"/api/v1/namespaces/{NS}/services"
JOBS = f"/apis/batch/v1/namespaces/{NS}/jobs"
PODS = f"/api/v1/namespaces/{NS}/pods"
SERVICE = f"{PREFIX}-churn-v2"
JOB = "bentloy-build-churn-v2-abc123"
DEPLOYMENT_PATH = f"{DEPLOYMENTS}/{SERVICE}"
JOB_PATH = f"{JOBS}/{JOB}"


class FakeResponse:
    """Minimal ``httpx.Response`` double with a status code and a JSON payload."""

    def __init__(self, payload: dict | None = None, status_code: int = 200) -> None:
        """Store the JSON payload and the HTTP status code."""
        self.status_code = status_code
        self._payload = payload if payload is not None else {}

    def json(self) -> dict:
        """Return the JSON payload."""
        return self._payload

    def raise_for_status(self) -> None:
        """Raise ``httpx.HTTPStatusError`` for 4xx and 5xx status codes."""
        if self.status_code >= 400:
            raise httpx.HTTPStatusError(
                "error",
                request=httpx.Request("GET", "http://k8s"),
                response=httpx.Response(self.status_code),
            )


class FakeContext:
    """Stand-in for the BentoML request context; handlers set ``response.status_code``."""

    def __init__(self) -> None:
        """Start with a 200 response."""
        self.response = FakeResponse()


class FakeAsyncClient:
    """``httpx.AsyncClient`` double used as an async context manager.

    Every call returns ``response``, or raises ``error`` when one is configured.
    """

    def __init__(self, response: FakeResponse | None = None, error: Exception | None = None) -> None:
        """Configure the canned response or the error to raise."""
        self.response = response
        self.error = error

    async def __aenter__(self) -> "FakeAsyncClient":
        """Enter the async context manager."""
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        """Leave the async context manager."""
        return None

    async def get(self, url: str, headers: dict[str, str] | None = None) -> FakeResponse:
        """Return the canned response or raise the configured error."""
        if self.error is not None:
            raise self.error
        assert self.response is not None
        return self.response


class FakeHealthClient(FakeAsyncClient):
    """Client double that answers ``/health`` calls per URL and can fail selected URLs."""

    def __init__(self) -> None:
        """Start with no failing URLs and no custom responses."""
        super().__init__()
        self.fail_urls: set[str] = set()
        self.responses: dict[str, dict] = {}

    async def get(self, url: str, headers: dict[str, str] | None = None) -> FakeResponse:
        """Raise for failing URLs; otherwise return the configured payload or ``{"status": "ok"}``."""
        if url in self.fail_urls:
            raise RuntimeError("health endpoint not reachable")
        return FakeResponse(self.responses.get(url, {"status": "ok"}))


class FakePredictClient(FakeAsyncClient):
    """Client double for model ``/predict`` calls: queued responses, echo fallback, timeouts."""

    def __init__(self) -> None:
        """Start with an empty response queue and no timeout."""
        super().__init__()
        self.responses: list[FakeResponse] = []
        self.raise_timeout = False

    async def post(self, url: str, json: dict) -> FakeResponse:
        """Raise a timeout, pop a queued response, or echo the request."""
        if self.raise_timeout:
            raise httpx.TimeoutException("timeout")
        if self.responses:
            return self.responses.pop(0)
        return FakeResponse({"ok": True, "url": url, "payload": json})


class FakeKubernetesApiClient:
    """Kubernetes client double that records resource creation and rollout waits."""

    def __init__(self) -> None:
        """Start with no recorded calls."""
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
        """Return empty logs for every container."""
        return ""

    async def ensure_resource(
        self,
        client: httpx.AsyncClient,
        headers: dict[str, str],
        list_url: str,
        resource_url: str,
        manifest: dict,
    ) -> None:
        """Record the resource that would have been created."""
        self.ensure_calls.append({"list_url": list_url, "resource_url": resource_url})

    async def wait_deployment_ready(
        self,
        client: httpx.AsyncClient,
        headers: dict[str, str],
        deployment_url: str,
        replicas: int,
        timeout_sec: int,
    ) -> None:
        """Record the rollout wait."""
        self.wait_calls.append(
            {
                "deployment_url": deployment_url,
                "replicas": replicas,
                "timeout_sec": timeout_sec,
            }
        )


class FakeDiscoveryService(KubernetesModelDiscovery):
    """Discovery double returning one fixed Service, as ``KubernetesModelDiscovery`` would."""

    def __init__(self) -> None:
        """Skip the parent constructor: the fake needs no configuration."""

    async def discover_services(
        self, base_url: str, headers: dict[str, str], verify: Any
    ) -> tuple[list[dict[str, str]], str | None]:
        """Return a single model Service and no error."""
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


class FakeTargetResolver:
    """Predict target resolver double that can be told to raise."""

    def __init__(self) -> None:
        """Start without failures."""
        self.raise_value_error = False
        self.raise_runtime_error = False

    def resolve_predict_target(self, model_name: str, model_url: str = ""):
        """Return a fixed target, or raise the configured error."""
        if self.raise_value_error:
            raise ValueError("invalid model_url")
        if self.raise_runtime_error:
            raise RuntimeError("network down")
        return f"http://model-{model_name}:3000/predict", f"http://model-{model_name}:3000", None


class FakeCluster:
    """In-memory Kubernetes API served through ``httpx.MockTransport``.

    Supports GET and DELETE of single resources, LIST of Deployments and Pods, and pod logs.
    ``routes`` overrides the answer for a given ``(method, path)``. Every request is recorded.
    In strict mode a request without a matching route is recorded in ``unexpected_requests``
    and raises ``AssertionError``; call ``assert_no_unexpected`` at the end of the test, since
    the code under test may swallow the exception.
    """

    def __init__(self, strict: bool = False) -> None:
        """Start with an empty cluster; ``strict`` rejects requests that have no route."""
        self.strict = strict
        self.unexpected_requests: list[str] = []
        self.resources: dict[str, dict] = {}
        self.deleted: list[str] = []
        self.pods: list[dict] = []
        self.pods_status = 200
        self.logs: dict[str, tuple[int, str]] = {}  # container -> (status, text)
        self.routes: dict[tuple[str, str], httpx.Response] = {}
        self.requests: list[httpx.Request] = []

    def add_deployment(self, name: str, image: str, operation_id: str | None = None) -> None:
        """Add a Deployment running ``image``, optionally owned by an operation."""
        annotations = {"bentloy.io/operation-id": operation_id} if operation_id is not None else {}
        self.resources[f"{DEPLOYMENTS}/{name}"] = {
            "metadata": {"name": name, "annotations": annotations},
            "spec": {"template": {"spec": {"containers": [{"name": "model", "image": image}]}}},
        }

    def add_service(self, name: str, operation_id: str | None = None) -> None:
        """Add a Service, optionally owned by an operation."""
        annotations = {"bentloy.io/operation-id": operation_id} if operation_id is not None else {}
        self.resources[f"{SERVICES}/{name}"] = {"metadata": {"name": name, "annotations": annotations}}

    def handler(self, request: httpx.Request) -> httpx.Response:
        """Answer a Kubernetes API request from the in-memory state."""
        self.requests.append(request)
        path = request.url.path
        route = self.routes.get((request.method, path))
        if route is not None:
            return route
        if self.strict:
            description = f"{request.method} {path}"
            self.unexpected_requests.append(description)
            raise AssertionError(f"Unexpected Kubernetes API request: {description}")
        if request.method == "GET" and path == PODS:
            return httpx.Response(self.pods_status, json={"items": self.pods})
        if request.method == "GET" and path.endswith("/log"):
            status, text = self.logs.get(request.url.params["container"], (404, ""))
            return httpx.Response(status, text=text)
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

    def assert_no_unexpected(self) -> None:
        """Fail if a strict cluster received a request that had no route."""
        assert not self.unexpected_requests, (
            f"Unexpected Kubernetes API requests: {self.unexpected_requests}"
        )

    @property
    def requested_paths(self) -> list[str]:
        """Return the path of every request received so far."""
        return [r.url.path for r in self.requests]

    def log_requests(self) -> list[httpx.Request]:
        """Return the pod-log requests received so far."""
        return [r for r in self.requests if r.url.path.endswith("/log")]

    def http_client_factory(self, verify: Any) -> httpx.AsyncClient:
        """Build an ``httpx.AsyncClient`` wired to this cluster (matches ``http_client_factory``)."""
        return httpx.AsyncClient(transport=httpx.MockTransport(self.handler))


def client_factory_for(handler: Callable[[httpx.Request], httpx.Response]) -> Callable[[Any], httpx.AsyncClient]:
    """Return an ``http_client_factory`` serving requests with a custom MockTransport handler."""

    def factory(verify: Any) -> httpx.AsyncClient:
        """Build an AsyncClient backed by ``handler``."""
        return httpx.AsyncClient(transport=httpx.MockTransport(handler))

    return factory


def build_request(job_name: str = "build-model-tiny-demo-abc123") -> DeployOperationRequest:
    """Build a deploy operation request for the ``tiny_demo_model`` model."""
    return {
        "namespace": NS,
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


def build_operation(
    operation_id: str,
    request: DeployOperationRequest,
    updated_at: str = "2026-04-15T10:00:00+00:00",
) -> DeployOperationRecord:
    """Build a queued operation record for ``request``."""
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


def make_pod(
    name: str,
    created: str,
    statuses: list[dict] | None = None,
    init_statuses: list[dict] | None = None,
    deleting: bool = False,
) -> dict:
    """Build a running Pod with optional container and init-container statuses."""
    metadata = {"name": name, "creationTimestamp": created}
    if deleting:
        metadata["deletionTimestamp"] = "2026-01-01T00:00:00Z"
    status: dict = {"phase": "Running"}
    if statuses is not None:
        status["containerStatuses"] = statuses
    if init_statuses is not None:
        status["initContainerStatuses"] = init_statuses
    return {"metadata": metadata, "status": status}
