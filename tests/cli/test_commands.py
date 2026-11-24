"""Tests for the ``up``, ``build``, ``deploy``, ``models`` and ``delete`` commands."""

import io
import json
from collections.abc import Callable
from unittest import mock

import pytest

import bentloy


def test_up_dry_run_runs_nothing(
    make_config: Callable[..., dict[str, str]], capsys: pytest.CaptureFixture[str]
) -> None:
    """``up --dry-run`` prints the manifest, runs no process and redacts credentials."""
    config = make_config(REGISTRY_USERNAME="user", REGISTRY_PASSWORD="secret-pw")
    args = bentloy.build_parser().parse_args(["up", "--dry-run"])
    with mock.patch("subprocess.run") as run, mock.patch("subprocess.Popen") as popen:
        bentloy.cmd_up(args, config)
    out = capsys.readouterr().out
    run.assert_not_called()
    popen.assert_not_called()
    assert "kind: Deployment" in out
    assert "secret-pw" not in out


def test_build_print_only(config: dict[str, str], capsys: pytest.CaptureFixture[str]) -> None:
    """``build --print-only`` prints the image refs without running anything."""
    args = bentloy.build_parser().parse_args(["build", "--print-only"])
    with mock.patch("subprocess.run") as run:
        bentloy.cmd_build(args, config)
    out = capsys.readouterr().out
    run.assert_not_called()
    assert "GATEWAY_IMAGE=registry.example.com/my-org/model-gateway:" in out
    assert "MODEL_BASE_IMAGE=registry.example.com/my-org/model-service-base:" in out


def test_build_skips_existing_and_builds_missing(
    monkeypatch: pytest.MonkeyPatch, config: dict[str, str]
) -> None:
    """Images already in the registry are skipped; missing ones are built and pushed."""
    images = bentloy.resolve_images(config)
    existing = images["gateway"]["ref"]
    commands = []
    monkeypatch.setattr(bentloy, "require_docker_daemon", lambda: None)
    monkeypatch.setattr(bentloy, "image_exists", lambda ref, insecure: ref == existing)
    monkeypatch.setattr(bentloy, "run", lambda cmd, **kw: commands.append(cmd))

    bentloy.build_images(config, images, force=False)

    base = images["model-base"]["ref"]
    assert commands == [
        ["docker", "build", "-t", base, str(images["model-base"]["context"])],
        ["docker", "push", base],
    ]


@pytest.mark.usefixtures("fake_gateway")
@pytest.mark.parametrize("name", ["Tiny_Demo_v2", "bentloy-model-tiny-demo-v2"])
def test_delete_accepts_deployed_or_service_name(
    monkeypatch: pytest.MonkeyPatch, config: dict[str, str], name: str
) -> None:
    """``delete`` resolves both the deployed name and the service name to the service name."""
    calls = []
    monkeypatch.setattr(bentloy, "call_gateway", lambda url, ep, payload: calls.append(payload))

    bentloy.cmd_delete(bentloy.build_parser().parse_args(["delete", name]), config)

    assert calls == [{"service_name": "bentloy-model-tiny-demo-v2"}]


@pytest.mark.usefixtures("fake_gateway")
def test_deploy_waits_until_ready(
    monkeypatch: pytest.MonkeyPatch, config: dict[str, str], capsys: pytest.CaptureFixture[str]
) -> None:
    """``deploy`` polls the status until the operation is ready."""
    responses = iter([
        {"operation_id": "op1", "deployed_name": "m", "image_ref": "img"},
        {"status": "building", "step": "create_job"},
        {"status": "ready", "step": "completed", "deployed_name": "m"},
    ])
    monkeypatch.setattr(bentloy, "call_gateway", lambda *a, **k: next(responses))
    monkeypatch.setattr(bentloy.time, "sleep", lambda *_: None)
    args = bentloy.build_parser().parse_args(["deploy", "m"])

    bentloy.cmd_deploy(args, config)

    assert 'model_name="m"' in capsys.readouterr().out


@pytest.mark.usefixtures("fake_gateway")
def test_deploy_failure_raises(monkeypatch: pytest.MonkeyPatch, config: dict[str, str]) -> None:
    """A failed operation makes ``deploy`` raise with the gateway's error."""
    responses = iter([
        {"operation_id": "op1", "deployed_name": "m", "image_ref": "img"},
        {"status": "failed", "step": "failed", "error": "boom"},
    ])
    monkeypatch.setattr(bentloy, "call_gateway", lambda *a, **k: next(responses))
    args = bentloy.build_parser().parse_args(["deploy", "m"])

    with pytest.raises(bentloy.CliError, match="boom"):
        bentloy.cmd_deploy(args, config)


@pytest.mark.usefixtures("fake_gateway")
def test_deploy_and_models_send_experiment_and_run(
    monkeypatch: pytest.MonkeyPatch, config: dict[str, str]
) -> None:
    """``deploy`` and ``models`` forward the experiment name, run id and model URI."""
    calls = []

    def fake_call(url: str, endpoint: str, payload: dict, retries: int = 0) -> dict:
        """Record the call and return a canned response."""
        calls.append((endpoint, payload))
        if endpoint == "deploy_model":
            return {"operation_id": "op1", "deployed_name": "m", "image_ref": "img"}
        return {"endpoints": {}}

    monkeypatch.setattr(bentloy, "call_gateway", fake_call)
    parser = bentloy.build_parser()
    run_id = "0123456789abcdef0123456789abcdef"

    bentloy.cmd_deploy(parser.parse_args([
        "deploy", "m", "--run-id", run_id, "--model-uri", f"runs:/{run_id}/ludwig-model",
        "--experiment-name", "Churn EU", "--no-wait",
    ]), config)
    bentloy.cmd_models(parser.parse_args([
        "models", "--experiment-name", "Churn EU", "--run-id", run_id,
    ]), config)

    deploy_payload = calls[0][1]
    assert deploy_payload["run_id"] == run_id
    assert deploy_payload["model_uri"] == f"runs:/{run_id}/ludwig-model"
    assert deploy_payload["experiment_name"] == "Churn EU"
    assert calls[1][0] == "list_models"
    assert calls[1][1]["experiment_name"] == "Churn EU"
    assert calls[1][1]["run_id"] == run_id
    assert calls[1][1]["page"] == 1


@pytest.mark.usefixtures("fake_gateway")
def test_deploy_overwrite_flag_is_sent(monkeypatch: pytest.MonkeyPatch, config: dict[str, str]) -> None:
    """``deploy`` sends ``overwrite`` as false by default and true with --overwrite."""
    calls = []

    def fake_call(url: str, endpoint: str, payload: dict, retries: int = 0) -> dict:
        """Record the payload and return a canned deploy response."""
        calls.append(payload)
        return {"operation_id": "op1", "deployed_name": "m", "image_ref": "img"}

    monkeypatch.setattr(bentloy, "call_gateway", fake_call)
    parser = bentloy.build_parser()

    bentloy.cmd_deploy(parser.parse_args(["deploy", "m", "--no-wait"]), config)
    bentloy.cmd_deploy(parser.parse_args(["deploy", "m", "--overwrite", "--no-wait"]), config)

    assert [c["overwrite"] for c in calls] == [False, True]


@pytest.mark.usefixtures("fake_gateway")
def test_deploy_conflict_shows_detail(monkeypatch: pytest.MonkeyPatch, config: dict[str, str]) -> None:
    """A 409 from the gateway is shown as a conflict with its detail and the --overwrite hint."""
    detail = ("'Churn V2' maps to bentloy-model-churn-v2 in namespace 'api-models', already "
              "used by run abc123. Use another name or overwrite=true.")
    body = json.dumps({"error": detail, "detail": detail}).encode()

    def conflict(*args, **kwargs):
        """Raise the HTTP 409 error returned by the gateway."""
        raise bentloy.urllib.error.HTTPError(
            "http://gw/deploy_model", 409, "Conflict", {}, io.BytesIO(body)
        )

    monkeypatch.setattr(bentloy.urllib.request, "urlopen", conflict)
    args = bentloy.build_parser().parse_args(["deploy", "m", "--name", "Churn V2"])

    with pytest.raises(bentloy.CliError) as caught:
        bentloy.cmd_deploy(args, config)

    message = str(caught.value)
    assert f"Deploy conflict: {detail}" in message
    assert "--overwrite" in message
    assert '"error"' not in message
