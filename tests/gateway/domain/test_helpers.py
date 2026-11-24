"""Tests for the shared gateway helpers."""

import pytest

from gateway.app.domain.helpers import (
    as_bool,
    is_valid_image_ref,
    is_valid_k8s_name,
    is_valid_registry_server,
    normalize_registry_server,
    positive_int_from_env,
    slug_to_model_name,
    to_model_slug,
)


@pytest.mark.parametrize(
    ("value", "default", "expected"),
    [
        ("true", False, True),
        ("1", False, True),
        ("false", True, False),
        ("0", True, False),
        ("unknown", True, True),
    ],
)
def test_as_bool(value: str, default: bool, expected: bool) -> None:
    """Known truthy and falsy strings are parsed; anything else uses the default."""
    assert as_bool(value, default=default) is expected


@pytest.mark.parametrize(
    ("key", "default", "expected"),
    [("A", 1, 10), ("B", 1, 1), ("C", 1, 1), ("MISSING", 7, 7)],
    ids=["valid", "negative", "not-a-number", "missing"],
)
def test_positive_int_from_env(key: str, default: int, expected: int) -> None:
    """Only positive integers are accepted; anything else falls back to the default."""
    env = {"A": "10", "B": "-5", "C": "abc"}
    assert positive_int_from_env(env, key, default) == expected


@pytest.mark.parametrize(
    "server", ["https://registry.example.com/api-models", "registry.example.com"]
)
def test_normalize_registry_server(server: str) -> None:
    """The registry server is reduced to its host name."""
    assert normalize_registry_server(server) == "registry.example.com"


@pytest.mark.parametrize(
    ("validator", "value", "expected"),
    [
        (is_valid_registry_server, "registry.example.com", True),
        (is_valid_registry_server, "https://registry.example.com", False),
        (is_valid_k8s_name, "model-tiny-demo-model", True),
        (is_valid_k8s_name, "Model_Upper", False),
        (is_valid_image_ref, "registry.example.com/api-models/model:tag", True),
        (is_valid_image_ref, "Docker.Invalid/Model", False),
    ],
)
def test_validators(validator, value: str, expected: bool) -> None:
    """Registry server, Kubernetes name and image reference validators accept and reject values."""
    assert validator(value) is expected


def test_slug_conversions() -> None:
    """Model names and slugs convert in both directions."""
    assert to_model_slug("Tiny_Demo_Model") == "tiny-demo-model"
    assert slug_to_model_name("tiny-demo-model") == "tiny_demo_model"
