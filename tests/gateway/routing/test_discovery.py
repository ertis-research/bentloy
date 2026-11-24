"""Tests for Kubernetes Service discovery of deployed models."""

from collections.abc import Callable

import pytest

from gateway.app.routing.discovery import KubernetesModelDiscovery
from tests.gateway.fakes import RUN_ID, FakeAsyncClient, FakeResponse

DiscoveryFactory = Callable[[FakeAsyncClient], KubernetesModelDiscovery]


@pytest.fixture
def make_discovery() -> DiscoveryFactory:
    """Return a factory building a discovery that talks to the given fake client."""

    def factory(client: FakeAsyncClient) -> KubernetesModelDiscovery:
        """Build a KubernetesModelDiscovery whose HTTP client is ``client``."""
        return KubernetesModelDiscovery(
            model_service_prefix="model",
            model_namespace="api-models",
            model_url_template="http://{service_name}:3000",
            client_factory=lambda **kwargs: client,
        )

    return factory


async def test_discover_services_success(make_discovery: DiscoveryFactory) -> None:
    """Only Services with the model prefix are returned, with null run metadata when absent."""
    payload = {
        "items": [
            {"metadata": {"name": "model-tiny-demo-model"}},
            {"metadata": {"name": "gateway"}},
        ]
    }
    discovery = make_discovery(FakeAsyncClient(FakeResponse(payload)))

    services, error = await discovery.discover_services(
        base_url="https://kubernetes",
        headers={"Authorization": "Bearer token"},
        verify=False,
    )

    assert error is None
    assert len(services) == 1
    assert services[0]["service_name"] == "model-tiny-demo-model"
    assert services[0]["inferred_model_name"] == "tiny_demo_model"
    # Services created before run/experiment metadata existed.
    assert services[0]["run_id"] is None
    assert services[0]["model_uri"] is None
    assert services[0]["experiment_name"] is None


async def test_discover_services_newest_first(make_discovery: DiscoveryFactory) -> None:
    """Services are sorted newest first, by name on ties, and undated ones last."""
    payload = {
        "items": [
            {"metadata": {"name": "model-old", "creationTimestamp": "2026-01-01T10:00:00Z"}},
            {"metadata": {"name": "model-undated"}},
            {"metadata": {"name": "model-new", "creationTimestamp": "2026-03-01T10:00:00Z"}},
            {"metadata": {"name": "model-b-same", "creationTimestamp": "2026-02-01T10:00:00Z"}},
            {"metadata": {"name": "model-a-same", "creationTimestamp": "2026-02-01T10:00:00Z"}},
        ]
    }
    discovery = make_discovery(FakeAsyncClient(FakeResponse(payload)))

    services, error = await discovery.discover_services(
        base_url="https://kubernetes", headers={}, verify=False
    )

    assert error is None
    assert [svc["service_name"] for svc in services] == [
        "model-new",
        "model-a-same",
        "model-b-same",
        "model-old",
        "model-undated",
    ]
    assert services[0]["created_at"] == "2026-03-01T10:00:00Z"
    assert services[-1]["created_at"] is None


async def test_discover_services_reads_run_and_experiment(make_discovery: DiscoveryFactory) -> None:
    """Run, model URI and experiment are read from labels and annotations; empty values are null."""
    payload = {
        "items": [
            {
                "metadata": {
                    "name": "model-churn-v2",
                    "labels": {
                        "app": "model-service",
                        "model-slug": "churn-v2",
                        "bentloy.io/run-id": RUN_ID,
                        "bentloy.io/experiment": "churn-prediction",
                    },
                    "annotations": {
                        "bentloy.io/model-uri": f"runs:/{RUN_ID}/ludwig-model",
                        "bentloy.io/experiment-name": "Churn Prediction",
                    },
                }
            },
            {
                "metadata": {
                    "name": "model-redeployed-without-run",
                    "labels": {"bentloy.io/run-id": ""},
                    "annotations": {"bentloy.io/model-uri": ""},
                }
            },
        ]
    }
    discovery = make_discovery(FakeAsyncClient(FakeResponse(payload)))

    services, error = await discovery.discover_services(
        base_url="https://kubernetes", headers={}, verify=False
    )

    assert error is None
    by_name = {svc["service_name"]: svc for svc in services}
    churn = by_name["model-churn-v2"]
    assert churn["run_id"] == RUN_ID
    assert churn["model_uri"] == f"runs:/{RUN_ID}/ludwig-model"
    assert churn["experiment_name"] == "Churn Prediction"
    assert churn["predict_endpoint"] == "http://model-churn-v2:3000/predict"
    empty = by_name["model-redeployed-without-run"]
    assert empty["run_id"] is None
    assert empty["model_uri"] is None
    assert empty["experiment_name"] is None


async def test_discover_services_error(make_discovery: DiscoveryFactory) -> None:
    """A client failure returns no services and an error message."""
    discovery = make_discovery(FakeAsyncClient(error=RuntimeError("network error")))

    services, error = await discovery.discover_services(
        base_url="https://kubernetes",
        headers={"Authorization": "Bearer token"},
        verify=False,
    )

    assert services == []
    assert error is not None
    assert "Could not discover Kubernetes services" in error
