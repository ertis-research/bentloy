"""Tests for PredictTargetResolver."""

import pytest

from gateway.app.routing.target_resolver import PredictTargetResolver


@pytest.fixture
def resolver() -> PredictTargetResolver:
    """Return a resolver with one mapped model and the ``model`` Service prefix."""
    return PredictTargetResolver(
        model_urls={"tiny_demo_model": "http://mapped-model:3000"},
        model_service_prefix="model",
        model_namespace="api-models",
        model_url_template="http://{service_name}:3000",
    )


def test_priority_explicit_model_url_overrides_mapping(resolver: PredictTargetResolver) -> None:
    """An explicit model_url wins over the environment mapping."""
    predict_url, base_url, source = resolver.resolve_predict_target(
        model_name="tiny_demo_model",
        model_url="http://external-host:3000",
    )
    assert source == "external_url"
    assert base_url == "http://external-host:3000"
    assert predict_url == "http://external-host:3000/predict"


def test_priority_env_mapping_over_k8s_convention(resolver: PredictTargetResolver) -> None:
    """The environment mapping wins over the Kubernetes naming convention."""
    predict_url, base_url, source = resolver.resolve_predict_target(model_name="tiny_demo_model")
    assert source == "env_mapping"
    assert base_url == "http://mapped-model:3000"
    assert predict_url == "http://mapped-model:3000/predict"


def test_fallback_to_k8s_convention(resolver: PredictTargetResolver) -> None:
    """Unmapped models resolve through the Kubernetes naming convention."""
    predict_url, base_url, source = resolver.resolve_predict_target(model_name="another_model")
    assert source == "k8s_convention"
    assert base_url == "http://model-another-model:3000"
    assert predict_url == "http://model-another-model:3000/predict"


def test_external_url_with_predict_suffix(resolver: PredictTargetResolver) -> None:
    """A trailing /predict in an external URL is not duplicated."""
    predict_url, base_url, source = resolver.resolve_predict_target(
        model_name="ignored",
        model_url="https://external-host/predict",
    )
    assert source == "external_url"
    assert base_url == "https://external-host"
    assert predict_url == "https://external-host/predict"


@pytest.mark.parametrize(
    "model_url",
    ["ftp://invalid-url", "http://external-host/path?bad=query"],
    ids=["unsupported-scheme", "path-and-query"],
)
def test_invalid_model_url_raises(resolver: PredictTargetResolver, model_url: str) -> None:
    """Unsupported schemes and URLs with paths or queries are rejected."""
    with pytest.raises(ValueError):
        resolver.resolve_predict_target(model_name="tiny_demo_model", model_url=model_url)
