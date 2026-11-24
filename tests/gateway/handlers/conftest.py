"""Fixtures for the gateway handler tests (build operations and info handler)."""

from collections.abc import Awaitable, Callable

import pytest

from gateway.app.deploy.operation_store import DeployOperationStore
from gateway.app.handlers.info_handler import InfoHandler
from tests.gateway.fakes import JOB, NS, FakeDiscoveryService


@pytest.fixture
def add_build_operation(
    operation_store: DeployOperationStore,
) -> Callable[..., Awaitable[None]]:
    """Return a coroutine function that adds the ``op_1`` build operation, with field overrides."""

    async def add(**changes) -> None:
        """Add a ``kaniko_push`` operation for the churn-v2 Job to the store."""
        operation = {
            "operation_id": "op_1",
            "status": "building",
            "step": "kaniko_push",
            "namespace": NS,
            "k8s_job": JOB,
            "request": {"namespace": NS, "deployment_slug": "churn-v2"},
        }
        operation.update(changes)
        await operation_store.add(operation)

    return add


@pytest.fixture
def make_info_handler(operation_store: DeployOperationStore) -> Callable[..., InfoHandler]:
    """Return a factory for InfoHandler instances with a fake discovery and the shared store."""

    def factory(**overrides) -> InfoHandler:
        """Build an InfoHandler; keyword overrides replace any constructor argument."""
        params = {
            "model_urls": {},
            "model_service_prefix": "model",
            "model_namespace": NS,
            "model_url_template": "http://{service_name}:3000",
            "recently_routed_models": {},
            "model_discovery": FakeDiscoveryService(),
            "operation_store": operation_store,
            "resolve_k8s_auth": lambda: ("https://k8s", {}, False),
        }
        params.update(overrides)
        return InfoHandler(**params)

    return factory
