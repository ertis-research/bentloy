"""Shared fixtures for the gateway tests."""

from collections.abc import Callable
from unittest.mock import AsyncMock

import pytest

from gateway.app.deploy.operation_store import DeployOperationStore
from gateway.app.deploy.orchestrator import DeployOrchestrator
from gateway.app.handlers.logs_handler import LogsHandler
from gateway.app.kubernetes.client import KubernetesApiClient
from gateway.service import GatewayService
from tests.gateway.fakes import (
    DEPLOYMENT_PATH,
    JOB,
    JOB_PATH,
    NS,
    PREFIX,
    FakeCluster,
    FakeContext,
)

BASE_IMAGE = "registry.example.com/api-models/base:py312"


@pytest.fixture
def fake_ctx() -> FakeContext:
    """Return a fake request context whose response status starts at 200."""
    return FakeContext()


@pytest.fixture
def operation_store() -> DeployOperationStore:
    """Return an empty in-memory operation store."""
    return DeployOperationStore()


@pytest.fixture
def k8s_cluster() -> FakeCluster:
    """Return an empty fake Kubernetes cluster served through httpx.MockTransport."""
    return FakeCluster()


@pytest.fixture
def logs_cluster(k8s_cluster: FakeCluster) -> FakeCluster:
    """Return a cluster that already has the model Deployment (1 replica) and its build Job."""
    k8s_cluster.resources[DEPLOYMENT_PATH] = {"spec": {"replicas": 1}}
    k8s_cluster.resources[JOB_PATH] = {"metadata": {"name": JOB}}
    return k8s_cluster


@pytest.fixture
def logs_handler(logs_cluster: FakeCluster, operation_store: DeployOperationStore) -> LogsHandler:
    """Return a LogsHandler talking to ``logs_cluster`` and sharing ``operation_store``."""
    return LogsHandler(
        resolve_k8s_auth=lambda: ("https://k8s", {}, False),
        k8s_client=KubernetesApiClient(),
        operation_store=operation_store,
        model_namespace=NS,
        model_service_prefix=PREFIX,
        http_client_factory=logs_cluster.http_client_factory,
    )


@pytest.fixture
def make_orchestrator(
    k8s_cluster: FakeCluster, operation_store: DeployOperationStore
) -> Callable[..., DeployOrchestrator]:
    """Return a factory for orchestrators wired to ``k8s_cluster``; keyword overrides replace any dependency."""

    def factory(**overrides) -> DeployOrchestrator:
        """Build a DeployOrchestrator with no-op manifest builders and the given overrides."""
        params = {
            "operation_store": operation_store,
            "k8s_client": KubernetesApiClient(),
            "resolve_k8s_auth": lambda: ("https://k8s", {}, False),
            "build_job_manifest": lambda **_: {},
            "build_delete_job_manifest": lambda **_: {},
            "build_deployment_manifest": lambda **_: {},
            "build_service_manifest": lambda **_: {},
            "kaniko_image": "kaniko",
            "model_service_prefix": "bentloy-model",
            "build_timeout_sec": 1,
            "rollout_timeout_sec": 1,
            "http_client_factory": k8s_cluster.http_client_factory,
        }
        params.update(overrides)
        return DeployOrchestrator(**params)

    return factory


@pytest.fixture
def gateway_service() -> GatewayService:
    """Return a GatewayService whose orchestrator and discovery are mocked and deploys are enabled."""
    service = GatewayService()
    service.model_base_image = BASE_IMAGE
    service.deploy_handler.model_base_image = BASE_IMAGE
    service.deploy_orchestrator.run_operation = AsyncMock(return_value=None)
    service.deploy_orchestrator.find_model_resource = AsyncMock(return_value=None)
    service.info_handler.discover_k8s_model_services = AsyncMock(return_value=([], None))
    return service
