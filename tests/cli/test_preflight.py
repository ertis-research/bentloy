"""Tests for the preflight checks run by ``bentloy up``."""

from collections.abc import Callable
from unittest import mock

import pytest

import bentloy


def test_docker_engine_not_running() -> None:
    """A failing ``docker info`` is reported as an engine that is not running."""
    failure = mock.Mock(
        returncode=1, stdout="", stderr="failed to connect to the docker API at npipe\nmore"
    )
    with (
        mock.patch.object(bentloy, "require_tool"),
        mock.patch.object(bentloy.subprocess, "run", return_value=failure),
        pytest.raises(bentloy.CliError, match=r"engine is not running[\s\S]*npipe"),
    ):
        bentloy.require_docker_daemon()


def test_cluster_unreachable() -> None:
    """An unreachable cluster raises a clear error."""
    failure = mock.Mock(returncode=1, stdout="", stderr="Unable to connect to the server")
    with (
        mock.patch.object(bentloy.subprocess, "run", return_value=failure),
        pytest.raises(bentloy.CliError, match="Cannot reach the Kubernetes cluster"),
    ):
        bentloy.namespace_exists("ns")


def test_namespace_not_found() -> None:
    """A NotFound error means the namespace does not exist."""
    missing = mock.Mock(
        returncode=1, stdout="", stderr='Error from server (NotFound): namespaces "ns" not found'
    )
    with mock.patch.object(bentloy.subprocess, "run", return_value=missing):
        assert bentloy.namespace_exists("ns") is False


@pytest.fixture
def run_up() -> Callable[..., list[str]]:
    """Return a runner for ``bentloy up`` with all external effects mocked; it returns the applied manifests."""

    def runner(
        config: dict[str, str], build: mock.Mock, namespace_exists: bool, secret_exists: bool
    ) -> list[str]:
        """Run ``cmd_up --no-wait`` and return the manifests passed to kubectl apply."""
        applied: list[str] = []
        with (
            mock.patch.object(bentloy, "require_tool"),
            mock.patch.object(bentloy, "namespace_exists", return_value=namespace_exists),
            mock.patch.object(bentloy, "secret_exists", return_value=secret_exists),
            mock.patch.object(bentloy, "build_images", build),
            mock.patch.object(bentloy, "run"),
            mock.patch.object(bentloy, "kubectl_apply", applied.append),
        ):
            bentloy.cmd_up(bentloy.build_parser().parse_args(["up", "--no-wait"]), config)
        return applied

    return runner


@pytest.mark.parametrize("namespace_exists", [True, False], ids=["namespace-exists", "namespace-missing"])
def test_missing_secret_fails_before_building(
    run_up: Callable[..., list[str]], config: dict[str, str], namespace_exists: bool
) -> None:
    """A missing registry secret without credentials fails before any image is built."""
    build = mock.Mock()
    with pytest.raises(bentloy.CliError, match="bentloy-registry-creds"):
        run_up(config, build, namespace_exists, secret_exists=False)
    build.assert_not_called()


def test_existing_secret_is_used_without_credentials(
    run_up: Callable[..., list[str]], config: dict[str, str]
) -> None:
    """An existing registry secret is reused and only the gateway manifest is applied."""
    build = mock.Mock()
    applied = run_up(config, build, namespace_exists=True, secret_exists=True)
    build.assert_called_once()
    assert len(applied) == 1
    assert "kind: Deployment" in applied[0]


def test_credentials_create_the_secret(
    run_up: Callable[..., list[str]], make_config: Callable[..., dict[str, str]]
) -> None:
    """Registry credentials make ``up`` apply the secret before the gateway manifest."""
    config = make_config(REGISTRY_USERNAME="u", REGISTRY_PASSWORD="p")
    build = mock.Mock()
    applied = run_up(config, build, namespace_exists=False, secret_exists=False)
    build.assert_called_once()
    assert len(applied) == 2
    assert "kind: Secret" in applied[0]
