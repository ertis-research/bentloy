"""Tests for CLI configuration loading and slug compatibility with the gateway."""

from pathlib import Path

import pytest

import bentloy
from gateway.app.domain.helpers import to_model_slug as gateway_slug


def test_env_file_and_environment_precedence(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Environment variables override the .env file, which overrides the defaults."""
    env_file = tmp_path / ".env"
    env_file.write_text(
        "# comment\nK8S_NAMESPACE=from-file\nREGISTRY_NAMESPACE='quoted-org'\nGATEWAY_PORT=9000\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("GATEWAY_PORT", "7000")

    config = bentloy.load_config(env_file)

    assert config["K8S_NAMESPACE"] == "from-file"
    assert config["REGISTRY_NAMESPACE"] == "quoted-org"
    assert config["GATEWAY_PORT"] == "7000"
    assert config["MODEL_REGISTRY_SECRET"] == "bentloy-registry-creds"


@pytest.mark.usefixtures("empty_environ")
def test_missing_env_file_uses_defaults() -> None:
    """A missing env file falls back to the default configuration."""
    config = bentloy.load_config(Path("does-not-exist.env"))
    assert config["K8S_NAMESPACE"] == "default"


def test_explicit_missing_env_file_fails(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """An explicit --env-file that does not exist is an error."""
    missing = tmp_path / "nope.env"

    code = bentloy.main(["--env-file", str(missing), "build"])

    assert code == 1
    assert f"Env file not found: {missing}" in capsys.readouterr().err


@pytest.mark.usefixtures("empty_environ")
def test_default_env_file_may_be_absent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """The default .env may be absent; the command then fails for missing settings instead."""
    monkeypatch.setattr(bentloy, "ROOT", tmp_path)

    code = bentloy.main(["build"])

    # Fails later (no REGISTRY_SERVER), but not because the default .env is missing.
    stderr = capsys.readouterr().err
    assert code == 1
    assert "Env file not found" not in stderr
    assert "Set REGISTRY_SERVER" in stderr


@pytest.mark.parametrize("name", ["Tiny_Demo_Model", "a__b--c", " model v2 ", "x.y_20260101"])
def test_slug_matches_gateway(name: str) -> None:
    """The CLI slug function matches the gateway's."""
    assert bentloy.to_model_slug(name) == gateway_slug(name)
