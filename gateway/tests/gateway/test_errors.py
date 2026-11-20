import unittest

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


class DeployErrorsTest(unittest.TestCase):
    def test_push_denied_is_summarized_with_hint(self) -> None:
        error = build_failure(
            prepare_logs="Model downloaded.\n",
            kaniko_logs=PUSH_DENIED_KANIKO_LOG,
            target_image="registry.example.com/bentloy-test-2/model-sasasa:vlatest-rc542505d",
            registry_secret="model-registry-creds",
        )
        self.assertTrue(error.message.startswith("Registry push denied for registry.example.com/"))
        self.assertIn("model-registry-creds", error.message)
        self.assertIn(
            "[kaniko] UNAUTHORIZED: unauthorized to access repository: bentloy-test-2/model-sasasa",
            error.message,
        )
        self.assertLessEqual(len(error.message), MAX_ERROR_CHARS)
        self.assertNotIn("\x1b[", error.message)
        # The full log keeps every line of both containers.
        assert error.log is not None
        self.assertIn("[prepare-build-context]\nModel downloaded.", error.log)
        self.assertIn("Retrieving image manifest", error.log)
        self.assertIn("action: push\n", error.log)

    def test_prepare_failure_uses_the_exception_line(self) -> None:
        error = build_failure(
            prepare_logs=MLFLOW_TRACEBACK,
            kaniko_logs="",
            target_image="reg/org/img:tag",
            registry_secret="creds",
        )
        self.assertEqual(
            error.message,
            "Build Job failed [prepare-build-context]: "
            "mlflow.exceptions.MlflowException: Run 'abc' not found",
        )
        assert error.log is not None
        self.assertIn("Traceback (most recent call last)", error.log)

    def test_no_logs(self) -> None:
        error = build_failure("", "", "reg/org/img:tag", "creds")
        self.assertEqual(error.message, "Build Job failed (no logs available).")

    def test_reason_falls_back_to_last_line(self) -> None:
        self.assertEqual(extract_failure_reason("step 1\nstep 2\n\n"), "step 2")
        self.assertEqual(extract_failure_reason(""), "")

    def test_truncate(self) -> None:
        self.assertEqual(truncate("short"), "short")
        long_text = "x" * 600
        self.assertEqual(len(truncate(long_text)), MAX_ERROR_CHARS)
        self.assertTrue(truncate(long_text).endswith("..."))


if __name__ == "__main__":
    unittest.main()
