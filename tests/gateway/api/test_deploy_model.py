"""Tests for ``/deploy_model`` and ``/deploy_status`` at the GatewayService level."""

import asyncio
import re

import pytest

from gateway.service import GatewayService
from tests.gateway.api.conftest import Deploy
from tests.gateway.fakes import RUN_ID, FakeContext


async def test_deploy_model_and_deploy_status_contract(gateway_service: GatewayService) -> None:
    """deploy_model queues an operation whose state deploy_status reports."""
    response = await gateway_service.deploy_model(
        model_name="tiny_demo_model",
        model_version="latest",
        tracking_uri="http://mlflow:5000",
        target_image="registry.example.com/api-models/model-tiny-demo:latest",
    )
    assert "operation_id" in response
    assert "k8s_job" in response
    assert response["deployed_name"] == "tiny_demo_model"

    operation_id = response["operation_id"]
    await asyncio.sleep(0)
    status = await gateway_service.deploy_status(operation_id)
    assert status["operation_id"] == operation_id
    assert status["status"] == "queued"
    assert status["deployed_name"] == "tiny_demo_model"

    await gateway_service.operation_store.update(operation_id, status="building", step="create_job")
    updated_status = await gateway_service.deploy_status(operation_id)
    assert updated_status["status"] == "building"
    assert updated_status["step"] == "create_job"


async def test_deploy_model_accepts_custom_deployed_name(gateway_service: GatewayService) -> None:
    """A custom deployed_name with append_date gets a date suffix in the name and the Job."""
    response = await gateway_service.deploy_model(
        model_name="tiny_demo_model",
        model_version="latest",
        tracking_uri="http://mlflow:5000",
        target_image="registry.example.com/api-models/model-tiny-demo:latest",
        deployed_name="tiny_alias",
        append_date=True,
    )

    assert re.search(r"^tiny_alias_[0-9]{8}$", response["deployed_name"])
    assert re.search(r"^bentloy-build-tiny-alias-[0-9]{8}-[a-z0-9]{6}$", response["k8s_job"])

    status = await gateway_service.deploy_status(response["operation_id"])
    assert re.search(r"^tiny_alias_[0-9]{8}$", status["deployed_name"])


async def test_deploy_model_stores_run_and_experiment(gateway_service: GatewayService) -> None:
    """Run id, model URI and experiment name are stored and exposed by status and listing."""
    model_uri = f"runs:/{RUN_ID}/ludwig-model"
    response = await gateway_service.deploy_model(
        model_name="churn_v2",
        run_id=RUN_ID,
        model_uri=model_uri,
        experiment_name="Churn prediction",
        target_image="registry.example.com/api-models/model-churn:latest",
    )
    assert response["run_id"] == RUN_ID
    assert response["model_uri"] == model_uri
    assert response["experiment_name"] == "Churn prediction"

    operation = await gateway_service.operation_store.get(response["operation_id"])
    assert operation is not None
    for record in (operation, operation["request"]):
        assert record["run_id"] == RUN_ID
        assert record["model_uri"] == model_uri
        assert record["experiment_name"] == "Churn prediction"

    status = await gateway_service.deploy_status(response["operation_id"])
    assert status["run_id"] == RUN_ID
    assert status["model_uri"] == model_uri
    assert status["experiment_name"] == "Churn prediction"

    listing = await gateway_service.list_models(experiment_name="Churn prediction")
    assert [op["operation_id"] for op in listing["deploy_operations"]] == [response["operation_id"]]
    assert listing["deploy_operations"][0]["run_id"] == RUN_ID
    assert listing["deploy_operations"][0]["model_uri"] == model_uri
    assert (await gateway_service.list_models(experiment_name="Other"))["deploy_operations"] == []


@pytest.mark.parametrize(
    ("expected", "kwargs"),
    [
        pytest.param("replicas must be >= 1.", {"replicas": 0}, id="replicas"),
        pytest.param("Invalid model_name", {"model_name": "!!!"}, id="model-name"),
        pytest.param("Invalid namespace", {"namespace": "Bad_NS"}, id="namespace"),
        pytest.param("Invalid run_id", {"run_id": "a" * 64}, id="run-id"),
        pytest.param("Invalid target_image", {"target_image": "UPPER/CASE"}, id="target-image"),
    ],
)
async def test_validation_errors_answer_400_with_detail(
    deploy: Deploy, expected: str, kwargs: dict
) -> None:
    """Invalid parameters answer 400 with matching ``error`` and ``detail`` and queue nothing."""
    status_code, body = await deploy(**kwargs)

    assert status_code == 400
    assert expected in body["detail"]
    assert body["error"] == body["detail"]
    assert "operation_id" not in body


@pytest.mark.parametrize("bad", ["-abcdef", "abcdef.", "ab c1", "abc"])
async def test_run_id_must_be_a_label_value(deploy: Deploy, bad: str) -> None:
    """A run_id that is not a valid label value answers 400."""
    status_code, _ = await deploy(run_id=bad)
    assert status_code == 400, bad


async def test_valid_run_id_is_accepted(deploy: Deploy) -> None:
    """A 32-character hexadecimal run_id queues the deployment."""
    status_code, body = await deploy(run_id=RUN_ID)
    assert status_code == 200
    assert "operation_id" in body


async def test_deployed_name_too_long_for_kubernetes(deploy: Deploy) -> None:
    """The longest deployed name that fits the 63-character limit is accepted; one more is not."""
    # bentloy-model- (14 chars) + 49-char slug = 63: accepted.
    status_code, body = await deploy(deployed_name="a" * 49)
    assert status_code == 200, body
    assert len(body["k8s_job"]) <= 63
    assert body["k8s_job"].startswith("bentloy-build-")
    status_code, body = await deploy(deployed_name="a" * 50)
    assert status_code == 400
    assert "deployed_name too long" in body["detail"]


async def test_disabled_deploy_answers_403(gateway_service: GatewayService, deploy: Deploy) -> None:
    """With auto deploy disabled the endpoint answers 403."""
    gateway_service.deploy_handler.auto_deploy_enabled = False

    status_code, body = await deploy()

    assert status_code == 403
    assert "disabled" in body["detail"]


@pytest.mark.parametrize(
    ("namespace", "target_prefix", "base_image"),
    [
        pytest.param(
            "api-models",
            "reg.example.com/api-models/model-tiny-demo-model:",
            "reg.example.com/api-models/model-service-base:py312",
            id="with-namespace",
        ),
        pytest.param(
            "",
            "reg.example.com/model-tiny-demo-model:",
            "reg.example.com/model-service-base:py312",
            id="without-namespace",
        ),
    ],
)
async def test_default_images_with_and_without_namespace(
    gateway_service: GatewayService, namespace: str, target_prefix: str, base_image: str
) -> None:
    """Without explicit images, the target and base images derive from the registry settings."""
    handler = gateway_service.deploy_handler
    handler.model_base_image = ""
    handler.registry_server = "reg.example.com"
    handler.registry_namespace = namespace
    ctx = FakeContext()

    body = await gateway_service.deploy_model(
        ctx=ctx, model_name="tiny_demo_model", tracking_uri="http://mlflow:5000"
    )

    assert ctx.response.status_code == 200, body
    assert body["image_ref"].startswith(target_prefix), body["image_ref"]
    operation = await gateway_service.operation_store.get(body["operation_id"])
    assert operation["request"]["base_image"] == base_image


async def test_gateway_misconfiguration_answers_500(
    gateway_service: GatewayService, deploy: Deploy
) -> None:
    """Missing registry settings and base image answer 500 naming MODEL_BASE_IMAGE."""
    gateway_service.deploy_handler.model_base_image = ""
    gateway_service.deploy_handler.registry_server = ""
    gateway_service.deploy_handler.registry_namespace = ""

    status_code, body = await deploy()

    assert status_code == 500
    assert "MODEL_BASE_IMAGE" in body["detail"]
