"""Tests for the conflict, overwrite and name-length rules of ``/deploy_model``."""

import asyncio

import pytest

from gateway.app.deploy.deploy_handler import CONFLICT_EXISTS, CONFLICT_IN_PROGRESS
from gateway.service import GatewayService
from tests.gateway.api.conftest import Deploy
from tests.gateway.fakes import RUN_ID, FakeContext

IN_PROGRESS_STATUSES = ["queued", "building", "pushing", "deploying", "deleting"]


def existing_service(run_id: str = RUN_ID, experiment_name: str = "Churn Prediction") -> dict:
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


@pytest.fixture
def deploy(gateway_service: GatewayService) -> Deploy:
    """Return a coroutine function deploying ``churn_model`` and returning ``(status_code, body)``."""

    async def call(**kwargs) -> tuple[int, dict]:
        """Deploy ``churn_model`` with the given overrides and capture the HTTP status."""
        ctx = FakeContext()
        params = {
            "model_name": "churn_model",
            "tracking_uri": "http://mlflow:5000",
            "target_image": "registry.example.com/api-models/model-churn:latest",
            **kwargs,
        }
        result = await gateway_service.deploy_model(ctx=ctx, **params)
        return ctx.response.status_code, result

    return call


async def test_new_name_deploys_as_before(gateway_service: GatewayService, deploy: Deploy) -> None:
    """A name with no existing resource is deployed after checking the Service name."""
    status_code, body = await deploy(deployed_name="Churn V2")

    assert status_code == 200, body
    assert "operation_id" in body
    gateway_service.deploy_orchestrator.find_model_resource.assert_awaited_once_with(
        gateway_service.deploy_handler.model_namespace, "bentloy-model-churn-v2"
    )


async def test_existing_service_answers_409_with_run_and_experiment(
    gateway_service: GatewayService, deploy: Deploy
) -> None:
    """An existing Service answers 409 describing the run and experiment that own it."""
    gateway_service.deploy_orchestrator.find_model_resource.return_value = existing_service()

    status_code, body = await deploy(deployed_name="Churn V2")

    assert status_code == 409
    assert body["error"] == body["detail"]
    for expected in ("'Churn V2'", "bentloy-model-churn-v2", RUN_ID, "'Churn Prediction'", "overwrite=true"):
        assert expected in body["detail"]
    assert body["conflict"] == CONFLICT_EXISTS
    assert body["conflict"] == "exists"
    assert body["resource_name"] == "bentloy-model-churn-v2"
    assert body["run_id"] == RUN_ID
    assert body["experiment_name"] == "Churn Prediction"
    assert "operation_id" not in body
    assert await gateway_service.operation_store.list_summaries() == []


async def test_existing_resource_without_metadata_answers_409(
    gateway_service: GatewayService, deploy: Deploy
) -> None:
    """A conflicting resource without run metadata answers 409 with null run and experiment."""
    gateway_service.deploy_orchestrator.find_model_resource.return_value = {
        "name": "bentloy-model-churn-v2"
    }

    status_code, body = await deploy(deployed_name="churn-v2")

    assert status_code == 409
    assert "an existing deployment" in body["detail"]
    assert body["error"] == body["detail"]
    assert body["conflict"] == "exists"
    assert body["resource_name"] == "bentloy-model-churn-v2"
    assert "run_id" in body
    assert body["run_id"] is None
    assert "experiment_name" in body
    assert body["experiment_name"] is None


async def test_existing_resource_with_empty_metadata_reports_null(
    gateway_service: GatewayService, deploy: Deploy
) -> None:
    """Keys present but empty (as the manifests write them when unknown) are reported as null."""
    resource = existing_service(run_id="", experiment_name="")
    resource["labels"].pop("bentloy.io/experiment")
    gateway_service.deploy_orchestrator.find_model_resource.return_value = resource

    _, body = await deploy(deployed_name="churn-v2")

    assert body["run_id"] is None
    assert body["experiment_name"] is None


async def test_names_with_the_same_slug_conflict(
    gateway_service: GatewayService, deploy: Deploy
) -> None:
    """Names that map to the same slug conflict, unless overwrite is requested."""
    status_code, body = await deploy(deployed_name="Churn_V2")
    assert status_code == 200, body
    # The first deploy has finished: its resources now exist in Kubernetes.
    await gateway_service.operation_store.update(body["operation_id"], status="ready")
    gateway_service.deploy_orchestrator.find_model_resource.return_value = existing_service()

    status_code, body = await deploy(deployed_name="churn v2")
    assert status_code == 409
    assert "'churn v2' maps to bentloy-model-churn-v2" in body["detail"]

    status_code, body = await deploy(deployed_name="churn v2", overwrite=True)
    assert status_code == 200, body
    assert body["deployed_name"] == "churn v2"


async def test_overwrite_skips_the_existence_check(
    gateway_service: GatewayService, deploy: Deploy
) -> None:
    """overwrite=true deploys over an existing resource without looking it up."""
    gateway_service.deploy_orchestrator.find_model_resource.return_value = existing_service()

    status_code, body = await deploy(deployed_name="Churn_V2", overwrite=True)

    assert status_code == 200, body
    gateway_service.deploy_orchestrator.find_model_resource.assert_not_awaited()


@pytest.mark.parametrize("overwrite", [False, True], ids=["no-overwrite", "overwrite"])
@pytest.mark.parametrize("status", IN_PROGRESS_STATUSES)
async def test_operation_in_progress_answers_409_even_with_overwrite(
    gateway_service: GatewayService, deploy: Deploy, status: str, overwrite: bool
) -> None:
    """An operation still acting on the same resource answers 409, with or without overwrite."""
    status_code, first = await deploy(
        deployed_name="Churn_V2", run_id=RUN_ID, experiment_name="Churn Prediction"
    )
    assert status_code == 200, first
    await gateway_service.operation_store.update(first["operation_id"], status=status)

    status_code, body = await deploy(deployed_name="churn v2", overwrite=overwrite)

    assert status_code == 409
    for expected in (first["operation_id"], f"'{status}'", RUN_ID, "'Churn Prediction'", "in progress"):
        assert expected in body["detail"]
    assert body["error"] == body["detail"]
    assert body["conflict"] == CONFLICT_IN_PROGRESS
    assert body["conflict"] == "in_progress"
    assert body["resource_name"] == "bentloy-model-churn-v2"
    assert body["operation_id"] == first["operation_id"]
    assert body["status"] == status


async def test_other_namespaces_and_finished_operations_do_not_block(
    gateway_service: GatewayService, deploy: Deploy
) -> None:
    """Operations in another namespace, or already finished, do not block a deployment."""
    _, first = await deploy(deployed_name="Churn_V2")
    await gateway_service.operation_store.update(first["operation_id"], status="building")

    status_code, body = await deploy(deployed_name="churn v2", namespace="other-ns")
    assert status_code == 200, body

    await gateway_service.operation_store.update(first["operation_id"], status="failed")
    status_code, body = await deploy(deployed_name="churn v2")
    assert status_code == 200, body


async def test_concurrent_requests_queue_only_one(
    gateway_service: GatewayService, deploy: Deploy
) -> None:
    """Of two concurrent deploys of the same slug, one is queued and the other gets 409."""
    results = await asyncio.gather(
        deploy(deployed_name="Churn_V2"),
        deploy(deployed_name="churn v2"),
    )

    assert sorted(code for code, _ in results) == [200, 409]
    assert len(await gateway_service.operation_store.list_summaries()) == 1


async def test_kubernetes_unreadable_answers_503(
    gateway_service: GatewayService, deploy: Deploy
) -> None:
    """If existing resources cannot be read, the deploy answers 503 and queues nothing."""
    gateway_service.deploy_orchestrator.find_model_resource.side_effect = RuntimeError(
        "Read failed (403): forbidden"
    )

    status_code, body = await deploy(deployed_name="Churn V2")

    assert status_code == 503
    assert "forbidden" in body["detail"]
    assert set(body) == {"error", "detail"}
    assert await gateway_service.operation_store.list_summaries() == []


@pytest.mark.parametrize(
    ("expected", "deployed_name"),
    [
        pytest.param("Invalid deployed_name", "!!! ___", id="empty-slug"),
        pytest.param("deployed_name too long", "a" * 50, id="too-long"),
    ],
)
async def test_empty_or_too_long_slug_answers_400(
    gateway_service: GatewayService, deploy: Deploy, expected: str, deployed_name: str
) -> None:
    """A deployed name with an empty or too long slug answers 400 with structured fields."""
    gateway_service.deploy_orchestrator.find_model_resource.return_value = existing_service()

    status_code, body = await deploy(overwrite=True, deployed_name=deployed_name)

    assert status_code == 400
    assert expected in body["detail"]
    assert body["error"] == body["detail"]
    assert body["invalid_field"] == "deployed_name"
    # 63 - len("bentloy-model-")
    assert body["max_length"] == 49
    gateway_service.deploy_orchestrator.find_model_resource.assert_not_awaited()


async def test_max_length_follows_the_service_prefix(
    gateway_service: GatewayService, deploy: Deploy
) -> None:
    """The maximum slug length depends on the configured Service prefix."""
    gateway_service.deploy_handler.model_service_prefix = "m"

    status_code, body = await deploy(deployed_name="a" * 61)
    assert status_code == 200, body
    status_code, body = await deploy(deployed_name="b" * 62)
    assert status_code == 400
    assert body["max_length"] == 61


async def test_other_validation_errors_have_no_structured_fields(deploy: Deploy) -> None:
    """Validation errors unrelated to the name only carry ``error`` and ``detail``."""
    status_code, body = await deploy(replicas=0)

    assert status_code == 400
    assert set(body) == {"error", "detail"}
