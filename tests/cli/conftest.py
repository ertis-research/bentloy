"""Shared fixtures for the bentloy.py CLI tests."""

import contextlib
import os
from collections.abc import Callable, Iterator

import pytest

import bentloy

BASE_ENV = {
    "REGISTRY_SERVER": "https://registry.example.com/ui",
    "REGISTRY_NAMESPACE": "my-org",
    "MLFLOW_TRACKING_URI": "http://mlflow:5000",
    "K8S_NAMESPACE": "api-models",
}


def _make_config(**overrides: str) -> dict[str, str]:
    """Build a CLI configuration from the defaults, the base test env and overrides."""
    config = dict(bentloy.DEFAULTS)
    config.update(BASE_ENV)
    config.update(overrides)
    return config


@pytest.fixture
def make_config() -> Callable[..., dict[str, str]]:
    """Return a factory that builds CLI configurations with optional overrides."""
    return _make_config


@pytest.fixture
def config() -> dict[str, str]:
    """Return the default CLI configuration used by the tests."""
    return _make_config()


@pytest.fixture
def empty_environ(monkeypatch: pytest.MonkeyPatch) -> None:
    """Remove every environment variable for the duration of the test."""
    for name in list(os.environ):
        monkeypatch.delenv(name)


@pytest.fixture
def fake_gateway(monkeypatch: pytest.MonkeyPatch) -> None:
    """Replace the gateway connection with a context manager yielding a placeholder URL."""

    @contextlib.contextmanager
    def fake_connection(config: dict[str, str], url: str | None) -> Iterator[str]:
        """Yield a fake gateway URL without opening any tunnel."""
        yield "http://gw"

    monkeypatch.setattr(bentloy, "gateway_connection", fake_connection)
