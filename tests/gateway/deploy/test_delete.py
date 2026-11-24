"""Tests for deleting operations, deployed models and their resources."""

from collections.abc import Callable
from unittest.mock import AsyncMock

import pytest

from gateway.app.deploy.deploy_handler import DeployHandler
from gateway.app.deploy.operation_store import DeployOperationStore
from gateway.app.deploy.orchestrator import DeployOrchestrator
from tests.gateway.fakes import DEPLOYMENTS, JOBS, NS, SERVICES, FakeCluster

IMAGE = "reg.example.com/api-models/model-churn:vlatest-r0123abcd"
OTHER_IMAGE = "reg.example.com/api-models/model-churn:v2-r9999ffff"


def _operation(
    operation_id: str = "op_1",
    deployed_name: str = "Churn V2",
    deployment_slug: str = "churn-v2",
    image_ref: str = IMAGE,
    status: str = "ready",
) -> dict:
    """Build an operation record deployed as ``bentloy-model-<deployment_slug>``."""
    return {
        "operation_id": operation_id,
        "status": status,
        "deployed_name": deployed_name,
        "namespace": NS,
        "image_ref": image_ref,
        "k8s_job": f"bentloy-build-{deployment_slug}-abc123",
        "request": {"namespace": NS, "deployment_slug": deployment_slug},
    }


@pytest.fixture
def orchestrator(make_orchestrator: Callable[..., DeployOrchestrator]) -> DeployOrchestrator:
    """Return an orchestrator over ``k8s_cluster`` whose registry image deletion is mocked."""
    orchestrator = make_orchestrator(registry_secret="creds")
    orchestrator._delete_registry_image = AsyncMock(return_value=None)
    return orchestrator


def _delete_image_kwargs(slug: str = "churn-v2") -> dict:
    """Return the expected keyword arguments of the registry image deletion."""
    return {
        "image_ref": IMAGE,
        "namespace": NS,
        "deployment_slug": slug,
        "registry_secret": "creds",
        "insecure_registry_tls": False,
    }


async def test_owned_resources_are_deleted_by_resource_name(
    orchestrator: DeployOrchestrator, k8s_cluster: FakeCluster
) -> None:
    """Resources owned by the operation are deleted by name, then the unused image."""
    k8s_cluster.add_deployment("bentloy-model-churn-v2", IMAGE, "op_1")
    k8s_cluster.add_service("bentloy-model-churn-v2", "op_1")
    k8s_cluster.resources[f"{JOBS}/bentloy-build-churn-v2-abc123"] = {"metadata": {}}

    notes = await orchestrator.delete_operation_resources(_operation(), "creds", False)

    assert k8s_cluster.deleted == [
        f"{DEPLOYMENTS}/bentloy-model-churn-v2",
        f"{SERVICES}/bentloy-model-churn-v2",
        f"{JOBS}/bentloy-build-churn-v2-abc123",
    ]
    # The delete-image Job is named from the slug, never from the raw deployed_name.
    orchestrator._delete_registry_image.assert_awaited_once_with(**_delete_image_kwargs())
    assert f"Image {IMAGE} deleted." in notes


async def test_resources_owned_by_another_operation_are_kept(
    orchestrator: DeployOrchestrator, k8s_cluster: FakeCluster
) -> None:
    """Resources now owned by another operation are kept, while this operation's image is deleted."""
    # op_1 failed or was overwritten: op_2 now owns the resource, with another image.
    k8s_cluster.add_deployment("bentloy-model-churn-v2", OTHER_IMAGE, "op_2")
    k8s_cluster.add_service("bentloy-model-churn-v2", "op_2")

    notes = await orchestrator.delete_operation_resources(_operation(), "creds", False)

    assert k8s_cluster.deleted == []
    assert "Deployment bentloy-model-churn-v2 kept: owned by op_2." in notes
    assert "Service bentloy-model-churn-v2 kept: owned by op_2." in notes
    # op_1's own image is not used by anyone: it is deleted.
    orchestrator._delete_registry_image.assert_awaited_once()


async def test_resources_without_operation_id_are_kept(
    orchestrator: DeployOrchestrator, k8s_cluster: FakeCluster
) -> None:
    """Resources deployed before the operation-id annotation existed are kept, with their image."""
    # Deployed before the annotation existed: cannot prove ownership, so keep it.
    k8s_cluster.add_deployment("bentloy-model-churn-v2", IMAGE)
    k8s_cluster.add_service("bentloy-model-churn-v2")

    notes = await orchestrator.delete_operation_resources(_operation(), "creds", False)

    assert k8s_cluster.deleted == []
    assert "a deploy without operation id" in notes[0]
    # ...and its image is the one that Deployment runs: kept too.
    orchestrator._delete_registry_image.assert_not_awaited()
    assert "still used by Deployment bentloy-model-churn-v2" in notes[-1]


async def test_deployed_name_matching_another_workload_does_not_touch_it(
    orchestrator: DeployOrchestrator, k8s_cluster: FakeCluster
) -> None:
    """A deployed name equal to an unrelated workload name never deletes that workload."""
    k8s_cluster.add_deployment("bentloy-gateway", "reg.example.com/gateway:1")
    k8s_cluster.add_service("bentloy-gateway")

    operation = _operation(deployed_name="bentloy-gateway", deployment_slug="bentloy-gateway")
    await orchestrator.delete_operation_resources(operation, "creds", False)

    assert f"{DEPLOYMENTS}/bentloy-gateway" in k8s_cluster.resources
    assert f"{SERVICES}/bentloy-gateway" in k8s_cluster.resources
    assert k8s_cluster.deleted == []


async def test_image_used_by_another_deployment_is_kept(
    orchestrator: DeployOrchestrator, k8s_cluster: FakeCluster
) -> None:
    """The image is kept while another Deployment still runs it."""
    k8s_cluster.add_deployment("bentloy-model-churn-v2", IMAGE, "op_1")
    k8s_cluster.add_service("bentloy-model-churn-v2", "op_1")
    k8s_cluster.add_deployment("bentloy-model-churn-prod", IMAGE, "op_9")

    notes = await orchestrator.delete_operation_resources(_operation(), "creds", False)

    assert f"{DEPLOYMENTS}/bentloy-model-churn-v2" in k8s_cluster.deleted
    orchestrator._delete_registry_image.assert_not_awaited()
    assert f"Image {IMAGE} kept: still used by Deployment bentloy-model-churn-prod." in notes


async def test_image_of_a_deploy_in_progress_is_kept(
    orchestrator: DeployOrchestrator, operation_store: DeployOperationStore
) -> None:
    """The image is kept while another operation is still deploying it."""
    await operation_store.add(
        _operation(
            operation_id="op_9", deployed_name="churn prod", deployment_slug="churn-prod",
            status="building",
        )
    )

    notes = await orchestrator.delete_operation_resources(_operation(), "creds", False)

    orchestrator._delete_registry_image.assert_not_awaited()
    assert "still used by operation op_9 (building)" in notes[-1]


async def test_finished_operations_do_not_keep_the_image(
    orchestrator: DeployOrchestrator, operation_store: DeployOperationStore
) -> None:
    """Failed or deleting operations do not keep the image alive."""
    await operation_store.add(_operation(operation_id="op_9", status="failed"))
    await operation_store.add(_operation(operation_id="op_1", status="deleting"))

    await orchestrator.delete_operation_resources(_operation(), "creds", False)

    orchestrator._delete_registry_image.assert_awaited_once()


async def test_listing_failure_keeps_the_image(orchestrator: DeployOrchestrator) -> None:
    """If Deployments cannot be listed, the error is raised and the image is not deleted."""
    orchestrator.k8s_client.list_resources = AsyncMock(side_effect=RuntimeError("List failed (403)"))

    with pytest.raises(RuntimeError, match="403"):
        await orchestrator.delete_operation_resources(_operation(), "creds", False)

    orchestrator._delete_registry_image.assert_not_awaited()


@pytest.mark.parametrize("name", ["bentloy-gateway", "kmlops", "bentloy-model-"])
async def test_rejects_names_outside_the_model_prefix(
    orchestrator: DeployOrchestrator, k8s_cluster: FakeCluster, name: str
) -> None:
    """Names outside the model prefix are rejected and nothing is deleted."""
    k8s_cluster.add_deployment("bentloy-gateway", "reg.example.com/gateway:1")

    result = await orchestrator.delete_deployed_model(name, NS)

    assert result["status"] == "error", name
    assert k8s_cluster.deleted == []


async def test_deletes_deployment_service_and_unused_image(
    orchestrator: DeployOrchestrator, k8s_cluster: FakeCluster
) -> None:
    """Deleting a model removes its Deployment, Service and the image nobody else uses."""
    k8s_cluster.add_deployment("bentloy-model-churn-v2", IMAGE)
    k8s_cluster.add_service("bentloy-model-churn-v2")

    result = await orchestrator.delete_deployed_model("bentloy-model-churn-v2", NS)

    assert result["status"] == "success"
    assert k8s_cluster.deleted == [
        f"{DEPLOYMENTS}/bentloy-model-churn-v2",
        f"{SERVICES}/bentloy-model-churn-v2",
    ]
    orchestrator._delete_registry_image.assert_awaited_once_with(**_delete_image_kwargs())


async def test_keeps_an_image_another_deployment_uses(
    orchestrator: DeployOrchestrator, k8s_cluster: FakeCluster
) -> None:
    """Deleting a model keeps an image another Deployment still runs."""
    k8s_cluster.add_deployment("bentloy-model-churn-v2", IMAGE)
    k8s_cluster.add_service("bentloy-model-churn-v2")
    k8s_cluster.add_deployment("bentloy-model-churn-prod", IMAGE)

    result = await orchestrator.delete_deployed_model("bentloy-model-churn-v2", NS)

    assert result["status"] == "success"
    assert "still used by Deployment bentloy-model-churn-prod" in result["message"]
    orchestrator._delete_registry_image.assert_not_awaited()
    assert f"{DEPLOYMENTS}/bentloy-model-churn-prod" in k8s_cluster.resources


async def test_orphan_service_is_deleted(
    orchestrator: DeployOrchestrator, k8s_cluster: FakeCluster
) -> None:
    """A Service without a Deployment is deleted and no image is touched."""
    k8s_cluster.add_service("bentloy-model-churn-v2")

    result = await orchestrator.delete_deployed_model("bentloy-model-churn-v2", NS)

    assert k8s_cluster.deleted == [f"{SERVICES}/bentloy-model-churn-v2"]
    assert result["status"] == "success"
    orchestrator._delete_registry_image.assert_not_awaited()


async def test_unknown_model(orchestrator: DeployOrchestrator) -> None:
    """Deleting a model that does not exist returns a not-found error."""
    result = await orchestrator.delete_deployed_model("bentloy-model-nope", NS)

    assert result["status"] == "error"
    assert "not found" in result["message"]


@pytest.fixture
def delete_handler(operation_store: DeployOperationStore) -> Callable[..., DeployHandler]:
    """Return a factory for DeployHandler instances over the shared store and a given orchestrator."""

    def factory(orchestrator: AsyncMock) -> DeployHandler:
        """Build a DeployHandler that deletes operations through ``orchestrator``."""
        return DeployHandler(
            auto_deploy_enabled=True,
            model_namespace=NS,
            default_tracking_uri="http://mlflow:5000",
            kaniko_skip_tls_verify_default=False,
            registry_server="reg.example.com",
            registry_namespace=NS,
            model_base_image="reg.example.com/base:py312",
            registry_secret="gateway-creds",
            operation_store=operation_store,
            deploy_orchestrator=orchestrator,
            now_iso=lambda: "2026-11-23T10:00:00+00:00",
        )

    return factory


async def test_success_removes_the_operation(
    delete_handler: Callable[..., DeployHandler], operation_store: DeployOperationStore
) -> None:
    """A successful deletion removes the operation, using the registry secret it was deployed with."""
    operation = _operation()
    operation["request"]["registry_secret"] = "op-creds"
    await operation_store.add(operation)
    orchestrator = AsyncMock()
    orchestrator.delete_operation_resources.return_value = ["Deployment x deleted."]

    await delete_handler(orchestrator).delete_operation_from_store(
        "op_1", operation_store, "gateway-creds"
    )

    assert await operation_store.get("op_1") is None
    orchestrator.delete_operation_resources.assert_awaited_once()
    kwargs = orchestrator.delete_operation_resources.await_args.kwargs
    assert kwargs["registry_secret"] == "op-creds"


async def test_failure_marks_delete_failed(
    delete_handler: Callable[..., DeployHandler], operation_store: DeployOperationStore
) -> None:
    """A failed deletion keeps the operation and marks it ``delete_failed`` with the error."""
    await operation_store.add(_operation())
    orchestrator = AsyncMock()
    orchestrator.delete_operation_resources.side_effect = RuntimeError("Delete failed (403)")

    await delete_handler(orchestrator).delete_operation_from_store(
        "op_1", operation_store, "gateway-creds"
    )

    operation = await operation_store.get("op_1")
    assert operation["status"] == "delete_failed"
    assert "403" in operation["error"]
