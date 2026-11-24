"""Tests for the deploy failure messages built from Job logs."""

from gateway.app.deploy.errors import (
    MAX_ERROR_CHARS,
    build_failure,
    extract_failure_reason,
    truncate,
)

PUSH_DENIED_KANIKO_LOG = (
    "\x1b[36mINFO\x1b[0m[0000] Retrieving image manifest python:3.12-slim\n"
    "\x1b[36mINFO\x1b[0m[0001] Retrieving image python:3.12-slim from registry index.docker.io\n"
    'error checking push permissions -- make sure you entered the correct tag name, and that '
    'you are authenticated correctly, and try again: checking push permission for '
    '"registry.example.com/bentloy-test-2/model-sasasa:vlatest-rc542505d": POST '
    "https://registry.example.com/v2/bentloy-test-2/model-sasasa/blobs/uploads/: UNAUTHORIZED: "
    "unauthorized to access repository: bentloy-test-2/model-sasasa, action: push: "
    "unauthorized to access repository: bentloy-test-2/model-sasasa, action: push\n"
)

MLFLOW_TRACEBACK = (
    "Downloading artifacts from runs:/abc/ludwig-model\n"
    "Traceback (most recent call last):\n"
    '  File "/prepare.py", line 10, in <module>\n'
    "    download()\n"
    "mlflow.exceptions.MlflowException: Run 'abc' not found\n"
)


def test_push_denied_is_summarized_with_hint() -> None:
    """A denied registry push gets a short message with a hint, and the full log is kept apart."""
    error = build_failure(
        prepare_logs="Model downloaded.\n",
        kaniko_logs=PUSH_DENIED_KANIKO_LOG,
        target_image="registry.example.com/bentloy-test-2/model-sasasa:vlatest-rc542505d",
        registry_secret="model-registry-creds",
    )
    assert error.message.startswith("Registry push denied for registry.example.com/")
    assert "model-registry-creds" in error.message
    assert (
        "[kaniko] UNAUTHORIZED: unauthorized to access repository: bentloy-test-2/model-sasasa"
        in error.message
    )
    assert len(error.message) <= MAX_ERROR_CHARS
    assert "\x1b[" not in error.message
    # The full log keeps every line of both containers.
    assert error.log is not None
    assert "[prepare-build-context]\nModel downloaded." in error.log
    assert "Retrieving image manifest" in error.log
    assert "action: push\n" in error.log


def test_prepare_failure_uses_the_exception_line() -> None:
    """A failure in the prepare container is summarized by its exception line."""
    error = build_failure(
        prepare_logs=MLFLOW_TRACEBACK,
        kaniko_logs="",
        target_image="reg/org/img:tag",
        registry_secret="creds",
    )
    assert error.message == (
        "Build Job failed [prepare-build-context]: "
        "mlflow.exceptions.MlflowException: Run 'abc' not found"
    )
    assert error.log is not None
    assert "Traceback (most recent call last)" in error.log


def test_no_logs() -> None:
    """Without logs the message says so."""
    error = build_failure("", "", "reg/org/img:tag", "creds")
    assert error.message == "Build Job failed (no logs available)."


def test_reason_falls_back_to_last_line() -> None:
    """Without a recognizable error, the last non-empty line is the reason."""
    assert extract_failure_reason("step 1\nstep 2\n\n") == "step 2"
    assert extract_failure_reason("") == ""


def test_truncate() -> None:
    """Long text is cut to MAX_ERROR_CHARS and ends with an ellipsis."""
    assert truncate("short") == "short"
    long_text = "x" * 600
    assert len(truncate(long_text)) == MAX_ERROR_CHARS
    assert truncate(long_text).endswith("...")
