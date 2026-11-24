"""Tests for InfoHandler (health and list_models) and the pagination helper."""

from collections.abc import Callable
from unittest.mock import AsyncMock

import pytest

from gateway.app.deploy.operation_store import DeployOperationStore
from gateway.app.handlers.info_handler import InfoHandler, ListModelsRequestError, paginate
from tests.gateway.fakes import FakeHealthClient

HandlerFactory = Callable[..., InfoHandler]

RUN_A1, RUN_A2, RUN_B = "a1" * 16, "a2" * 16, "b1" * 16


def services_named(*names: str) -> list[dict]:
    """Build discovered Service entries named ``model-<name>``."""
    return [
        {
            "service_name": f"model-{name}",
            "inferred_model_name": name,
            "predict_endpoint": f"http://model-{name}:3000/predict",
        }
        for name in names
    ]


def _churn_service(name: str, run_id: str | None, experiment: str | None) -> dict:
    """Build a discovered Service that carries run and experiment metadata."""
    return {
        "service_name": f"model-{name}",
        "model_slug": name,
        "inferred_model_name": name.replace("-", "_"),
        "base_url": f"http://model-{name}:3000",
        "predict_endpoint": f"http://model-{name}:3000/predict",
        "run_id": run_id,
        "model_uri": f"runs:/{run_id}/ludwig-model" if run_id else None,
        "experiment_name": experiment,
    }


@pytest.fixture
def paged_handler(make_info_handler: HandlerFactory) -> HandlerFactory:
    """Return a factory for a handler with 3 configured models, given services and no operations."""

    def factory(services: list[dict]) -> InfoHandler:
        """Build the handler and stub its Kubernetes discovery to return ``services``."""
        handler = make_info_handler(
            model_urls={name: f"http://model-{name}:3000" for name in ("a", "b", "c")}
        )
        handler.discover_k8s_model_services = AsyncMock(return_value=(services, None))  # type: ignore[method-assign]
        return handler

    return factory


@pytest.fixture
async def churn_handler(
    make_info_handler: HandlerFactory, operation_store: DeployOperationStore
) -> InfoHandler:
    """Return a handler with four discovered services and three ready operations (two experiments)."""
    services = [
        _churn_service("churn-v1", RUN_A1, "Churn"),
        _churn_service("churn-v2", RUN_A2, "Churn"),
        _churn_service("churn-eu", RUN_B, "Churn EU"),
        _churn_service("legacy", None, None),
    ]
    for op_id, run_id, experiment in (
        ("op_a1", RUN_A1, "Churn"),
        ("op_a2", RUN_A2, "Churn"),
        ("op_b", RUN_B, "Churn EU"),
    ):
        await operation_store.add(
            {  # type: ignore[typeddict-item]
                "operation_id": op_id,
                "status": "ready",
                "updated_at": f"2026-04-15T12:00:0{len(op_id)}+00:00",
                "model_name": op_id,
                "deployed_name": op_id,
                "image_ref": "registry/org/img:tag",
                "run_id": run_id,
                "model_uri": f"runs:/{run_id}/ludwig-model",
                "experiment_name": experiment,
            }
        )
    handler = make_info_handler(
        model_urls={"a": "http://model-a:3000"},
        recently_routed_models={"z": "http://model-z:3000"},
    )
    handler.discover_k8s_model_services = AsyncMock(return_value=(services, None))  # type: ignore[method-assign]
    return handler


async def test_health_contract(make_info_handler: HandlerFactory) -> None:
    """Health reports the gateway and each configured model, marking unreachable ones unhealthy."""
    fake_client = FakeHealthClient()
    fake_client.responses["http://model-a:3000/health"] = {"status": "healthy"}
    fake_client.fail_urls.add("http://model-b:3000/health")
    handler = make_info_handler(
        model_urls={"a": "http://model-a:3000", "b": "http://model-b:3000"},
        resolve_k8s_auth=lambda: ("https://k8s", {"Authorization": "Bearer x"}, False),
        client_factory=lambda timeout: fake_client,
    )

    result = await handler.health()

    assert result["gateway"] == "healthy"
    assert result["models"]["a"] == {"status": "healthy"}
    assert result["models"]["b"]["status"] == "unhealthy"


async def test_list_models_contract_and_discovery_error(
    make_info_handler: HandlerFactory, operation_store: DeployOperationStore
) -> None:
    """list_models reports a discovery error and still paginates and filters the other lists."""
    await operation_store.add(
        {
            "operation_id": "op_1",
            "status": "queued",
            "step": "queued",
            "created_at": "2026-04-15T11:59:59+00:00",
            "updated_at": "2026-04-15T11:59:59+00:00",
            "model_name": "tiny_demo_model",
            "deployed_name": "tiny_demo_model",
            "model_version": "latest",
            "namespace": "api-models",
            "replicas": 1,
            "image_ref": "registry.example.com/api-models/model-tiny-demo:latest",
            "k8s_job": "build-model-tiny-demo",
            "error": None,
            "message": None,
            "request": {
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
                "job_name": "build-model-tiny-demo",
            },
        }
    )
    handler = make_info_handler(
        model_urls={"a": "http://model-a:3000"},
        recently_routed_models={"z": "http://model-z:3000"},
    )
    handler.discover_k8s_model_services = AsyncMock(  # type: ignore[method-assign]
        return_value=([], "simulated discovery error")
    )

    result = await handler.list_models(page=1, page_size=10, query="")

    assert result["discovery"]["status"] == "error"
    assert result["discovery"]["error"] == "simulated discovery error"
    assert result["configured_models"] == ["a"]
    assert result["recently_routed_models"] == ["z"]
    assert len(result["deploy_operations"]) == 1
    assert result["pagination"]["configured_models"]["total_items"] == 1
    assert not result["pagination"]["configured_models"]["has_next"]

    filtered = await handler.list_models(page=1, page_size=10, query="zz")
    assert filtered["configured_models"] == []
    assert filtered["recently_routed_models"] == []

    second_page = await handler.list_models(page=2, page_size=1, query="")
    assert second_page["configured_models"] == []
    assert second_page["pagination"]["configured_models"]["has_previous"]


async def test_list_models_without_filters_returns_everything(churn_handler: InfoHandler) -> None:
    """Without filters every service, operation and configured model is listed."""
    result = await churn_handler.list_models(page=1, page_size=10, query="")

    assert len(result["discovered_models"]) == 4
    assert len(result["deploy_operations"]) == 3
    assert result["configured_models"] == ["a"]


async def test_list_models_experiment_filter_is_exact(churn_handler: InfoHandler) -> None:
    """The experiment filter is exact: "Churn" does not match "Churn EU"."""
    result = await churn_handler.list_models(
        page=1, page_size=10, query="", experiment_name="Churn"
    )

    assert [svc["service_name"] for svc in result["discovered_models"]] == [
        "model-churn-v1",
        "model-churn-v2",
    ]
    assert sorted(op["operation_id"] for op in result["deploy_operations"]) == ["op_a1", "op_a2"]
    assert result["configured_models"] == []
    assert result["recently_routed_models"] == []
    assert sorted(result["endpoints"]) == ["churn_v1", "churn_v2"]
    assert result["pagination"]["discovered_models"]["total_items"] == 2


async def test_list_models_pagination_applies_to_filtered_result(churn_handler: InfoHandler) -> None:
    """Pagination is computed over the filtered list."""
    page_2 = await churn_handler.list_models(
        page=2, page_size=1, query="", experiment_name="Churn"
    )

    assert [svc["service_name"] for svc in page_2["discovered_models"]] == ["model-churn-v2"]
    assert not page_2["pagination"]["discovered_models"]["has_next"]


async def test_list_models_run_filter(churn_handler: InfoHandler) -> None:
    """The run filter selects a single version."""
    result = await churn_handler.list_models(page=1, page_size=10, query="", run_id=RUN_A2)

    assert [svc["service_name"] for svc in result["discovered_models"]] == ["model-churn-v2"]
    assert [op["operation_id"] for op in result["deploy_operations"]] == ["op_a2"]


async def test_list_models_run_and_experiment_filters_combine(churn_handler: InfoHandler) -> None:
    """Run and experiment filters are ANDed: a run outside the experiment matches nothing."""
    result = await churn_handler.list_models(
        page=1, page_size=10, query="", experiment_name="Churn EU", run_id=RUN_A2
    )

    assert result["discovered_models"] == []
    assert result["deploy_operations"] == []


async def test_query_filters_deploy_operations_by_deployed_name(
    make_info_handler: HandlerFactory, operation_store: DeployOperationStore
) -> None:
    """The query filters operations by deployed name, case-insensitively."""
    for op_id, deployed_name in (("op_1", "E2E_QA"), ("op_2", "e2e_qb"), ("op_3", "churn")):
        await operation_store.add(
            {  # type: ignore[typeddict-item]
                "operation_id": op_id,
                "status": "ready",
                "updated_at": "2026-04-15T12:00:00+00:00",
                "model_name": "base_model",
                "deployed_name": deployed_name,
                "image_ref": "registry/org/img:tag",
            }
        )
    handler = make_info_handler()
    handler.discover_k8s_model_services = AsyncMock(return_value=([], None))  # type: ignore[method-assign]

    result = await handler.list_models(page=1, page_size=10, query="e2e_q")
    assert sorted(op["operation_id"] for op in result["deploy_operations"]) == ["op_1", "op_2"]
    assert result["pagination"]["deploy_operations"]["total_items"] == 2
    result = await handler.list_models(page=1, page_size=10, query="e2e_qa")
    assert [op["operation_id"] for op in result["deploy_operations"]] == ["op_1"]
    result = await handler.list_models(page=1, page_size=10, query="")
    assert len(result["deploy_operations"]) == 3


async def test_first_page_is_page_one(paged_handler: Callable[..., InfoHandler]) -> None:
    """page=1 returns the first items of every list."""
    handler = paged_handler(services_named("x", "y", "z"))

    first = await handler.list_models(page=1, page_size=2, query="")

    assert first["configured_models"] == ["a", "b"]
    assert [s["service_name"] for s in first["discovered_models"]] == ["model-x", "model-y"]
    assert list(first["endpoints"]) == ["x", "y"]
    meta = first["pagination"]["configured_models"]
    assert meta["current_page"] == 1
    assert meta["total_pages"] == 2
    assert meta["total_items"] == 3
    assert meta["has_next"]
    assert not meta["has_previous"]


async def test_second_page_is_the_last(paged_handler: Callable[..., InfoHandler]) -> None:
    """page=2 returns the next items and is the last page of the shorter lists."""
    handler = paged_handler(services_named("x", "y", "z"))

    second = await handler.list_models(page=2, page_size=2, query="")

    assert second["configured_models"] == ["c"]
    assert [s["service_name"] for s in second["discovered_models"]] == ["model-z"]
    assert list(second["endpoints"]) == ["z", "a"]
    for key in ("configured_models", "discovered_models"):
        meta = second["pagination"][key]
        assert meta["current_page"] == 2, key
        assert not meta["has_next"], key
        assert meta["has_previous"], key
    # endpoints has 6 items (3 discovered + 3 configured): page 2 of 3.
    assert second["pagination"]["endpoints"]["has_next"]


async def test_out_of_range_page_metadata(paged_handler: Callable[..., InfoHandler]) -> None:
    """An out-of-range page is an empty slice, not clamped, with a previous page but no next."""
    handler = paged_handler([])

    result = await handler.list_models(page=5, page_size=2, query="")

    pagination = result["pagination"]
    assert result["configured_models"] == []
    assert result["endpoints"] == {}
    assert pagination["configured_models"]["current_page"] == 5
    assert pagination["configured_models"]["total_pages"] == 2
    assert not pagination["configured_models"]["has_next"]
    assert pagination["configured_models"]["has_previous"]


async def test_empty_list_has_zero_pages(paged_handler: Callable[..., InfoHandler]) -> None:
    """Empty lists report zero items and pages and no neighbours."""
    handler = paged_handler([])

    result = await handler.list_models(page=1, page_size=2, query="")

    for key in ("recently_routed_models", "discovered_models", "deploy_operations"):
        meta = result["pagination"][key]
        assert meta["total_items"] == 0, key
        assert meta["total_pages"] == 0, key
        assert not meta["has_next"], key
        assert not meta["has_previous"], key


@pytest.mark.parametrize("page", [0, -1])
async def test_page_below_one_is_rejected(paged_handler: Callable[..., InfoHandler], page: int) -> None:
    """Pages below 1 raise a 400 request error."""
    handler = paged_handler([])

    with pytest.raises(ListModelsRequestError) as caught:
        await handler.list_models(page=page, page_size=2, query="")

    assert caught.value.status_code == 400
    assert "page must be >= 1" in caught.value.message


def test_paginate_slices_and_metadata() -> None:
    """paginate returns 1-indexed slices and matching metadata."""
    items = list(range(5))
    assert paginate(items, 1, 2)[0] == [0, 1]
    assert paginate(items, 3, 2)[0] == [4]
    assert paginate(items, 4, 2)[0] == []
    assert paginate(items, 3, 2)[1] == {
        "total_items": 5,
        "total_pages": 3,
        "current_page": 3,
        "page_size": 2,
        "has_next": False,
        "has_previous": True,
    }
    assert paginate([], 1, 10)[1]["total_pages"] == 0
