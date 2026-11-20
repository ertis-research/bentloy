import base64
import contextlib
import io
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import yaml

import bentloy
from gateway.app.domain.helpers import to_model_slug as gateway_slug

BASE_ENV = {
    "REGISTRY_SERVER": "https://registry.example.com/ui",
    "REGISTRY_NAMESPACE": "my-org",
    "MLFLOW_TRACKING_URI": "http://mlflow:5000",
    "K8S_NAMESPACE": "api-models",
}


def make_config(**overrides: str) -> dict[str, str]:
    config = dict(bentloy.DEFAULTS)
    config.update(BASE_ENV)
    config.update(overrides)
    return config


class ConfigTest(unittest.TestCase):
    def test_env_file_and_environment_precedence(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            env_file = Path(tmp) / ".env"
            env_file.write_text(
                "# comment\nK8S_NAMESPACE=from-file\nREGISTRY_NAMESPACE='quoted-org'\nGATEWAY_PORT=9000\n",
                encoding="utf-8",
            )
            with mock.patch.dict(os.environ, {"GATEWAY_PORT": "7000"}, clear=False):
                config = bentloy.load_config(env_file)

        self.assertEqual(config["K8S_NAMESPACE"], "from-file")
        self.assertEqual(config["REGISTRY_NAMESPACE"], "quoted-org")
        self.assertEqual(config["GATEWAY_PORT"], "7000")
        self.assertEqual(config["MODEL_REGISTRY_SECRET"], "bentloy-registry-creds")

    def test_missing_env_file_uses_defaults(self) -> None:
        with mock.patch.dict(os.environ, {}, clear=True):
            config = bentloy.load_config(Path("does-not-exist.env"))
        self.assertEqual(config["K8S_NAMESPACE"], "default")

    def test_explicit_missing_env_file_fails(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            missing = Path(tmp) / "nope.env"
            stderr = io.StringIO()
            with contextlib.redirect_stderr(stderr):
                code = bentloy.main(["--env-file", str(missing), "build"])
        self.assertEqual(code, 1)
        self.assertIn(f"Env file not found: {missing}", stderr.getvalue())

    def test_default_env_file_may_be_absent(self) -> None:
        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(bentloy, "ROOT", Path(tmp)):
            stderr = io.StringIO()
            with mock.patch.dict(os.environ, {}, clear=True), contextlib.redirect_stderr(stderr):
                code = bentloy.main(["build"])
        # Fails later (no REGISTRY_SERVER), but not because the default .env is missing.
        self.assertEqual(code, 1)
        self.assertNotIn("Env file not found", stderr.getvalue())
        self.assertIn("Set REGISTRY_SERVER", stderr.getvalue())

    def test_slug_matches_gateway(self) -> None:
        for name in ["Tiny_Demo_Model", "a__b--c", " model v2 ", "x.y_20260101"]:
            self.assertEqual(bentloy.to_model_slug(name), gateway_slug(name))


class ImagesTest(unittest.TestCase):
    def test_tags_are_content_hashes(self) -> None:
        images = bentloy.resolve_images(make_config())
        gateway_ref = images["gateway"]["ref"]
        base_ref = images["model-base"]["ref"]
        self.assertRegex(gateway_ref, r"^registry\.example\.com/my-org/model-gateway:[0-9a-f]{12}$")
        self.assertRegex(base_ref, r"^registry\.example\.com/my-org/model-service-base:py312-[0-9a-f]{12}$")
        self.assertEqual(images, bentloy.resolve_images(make_config()))

    def test_refs_without_namespace(self) -> None:
        images = bentloy.resolve_images(make_config(REGISTRY_NAMESPACE=""))
        self.assertRegex(images["gateway"]["ref"], r"^registry\.example\.com/model-gateway:[0-9a-f]{12}$")
        self.assertRegex(
            images["model-base"]["ref"], r"^registry\.example\.com/model-service-base:py312-[0-9a-f]{12}$"
        )

    def test_registry_is_required_but_namespace_is_not(self) -> None:
        with self.assertRaisesRegex(bentloy.CliError, "Set REGISTRY_SERVER in .env"):
            bentloy.resolve_images(make_config(REGISTRY_SERVER=""))

    def test_overrides_are_not_built(self) -> None:
        images = bentloy.resolve_images(make_config(GATEWAY_IMAGE="reg/x/gw:1"))
        self.assertEqual(images["gateway"], {"ref": "reg/x/gw:1", "build": False,
                                             "context": bentloy.IMAGES["gateway"]["context"]})
        self.assertTrue(images["model-base"]["build"])

    def test_missing_registry_fails(self) -> None:
        with self.assertRaises(bentloy.CliError):
            bentloy.resolve_images(make_config(REGISTRY_SERVER=""))

    def test_source_hash_ignores_caches_and_line_endings(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            ctx = Path(tmp)
            (ctx / "app").mkdir()
            (ctx / "Dockerfile").write_bytes(b"FROM x\nRUN y\n")
            (ctx / "app" / "m.py").write_bytes(b"a = 1\n")
            first = bentloy.source_hash(ctx)

            (ctx / "Dockerfile").write_bytes(b"FROM x\r\nRUN y\r\n")
            (ctx / "app" / "__pycache__").mkdir()
            (ctx / "app" / "__pycache__" / "m.cpython-312.pyc").write_bytes(b"junk")
            (ctx / "tests").mkdir()
            (ctx / "tests" / "t.py").write_bytes(b"not in image")
            self.assertEqual(bentloy.source_hash(ctx), first)

            (ctx / "app" / "m.py").write_bytes(b"a = 2\n")
            self.assertNotEqual(bentloy.source_hash(ctx), first)


class ManifestsTest(unittest.TestCase):
    def test_gateway_manifest_renders_valid_yaml(self) -> None:
        config = make_config(MLFLOW_TRACKING_URI='http://h:5000/?a=1&b="2"')
        images = bentloy.resolve_images(config)
        manifest = bentloy.render("gateway.yaml", bentloy.gateway_values(config, images))
        docs = {doc["kind"]: doc for doc in yaml.safe_load_all(manifest) if doc}

        self.assertEqual(
            set(docs), {"ServiceAccount", "Role", "RoleBinding", "ConfigMap", "Deployment", "Service"}
        )
        for doc in docs.values():
            self.assertEqual(doc["metadata"]["namespace"], "api-models")
            # Everything Bentloy creates is prefixed with "bentloy-".
            self.assertTrue(doc["metadata"]["name"].startswith("bentloy-"), doc["metadata"]["name"])
        self.assertEqual(docs["Service"]["metadata"]["name"], "bentloy-gateway")
        self.assertEqual(
            docs["Deployment"]["spec"]["template"]["spec"]["serviceAccountName"], "bentloy-gateway-sa"
        )
        self.assertEqual(docs["RoleBinding"]["roleRef"]["name"], docs["Role"]["metadata"]["name"])
        self.assertEqual(docs["ConfigMap"]["data"]["MODEL_SERVICE_PREFIX"], "bentloy-model")
        data = docs["ConfigMap"]["data"]
        self.assertEqual(data["K8S_NAMESPACE"], "api-models")
        self.assertEqual(data["MLFLOW_TRACKING_URI"], 'http://h:5000/?a=1&b="2"')
        self.assertEqual(data["REGISTRY_SERVER"], "registry.example.com")
        self.assertEqual(data["MODEL_BASE_IMAGE"], images["model-base"]["ref"])
        self.assertTrue(all(isinstance(value, str) for value in data.values()))
        self.assertEqual(data["KANIKO_CACHE"], "1")
        self.assertEqual(data["MODEL_PIP_EXTRA_INDEX_URL"], "https://download.pytorch.org/whl/cpu")
        disabled = make_config(MODEL_PIP_EXTRA_INDEX_URL="", KANIKO_CACHE="0")
        disabled_docs = yaml.safe_load_all(
            bentloy.render("gateway.yaml", bentloy.gateway_values(disabled, images))
        )
        disabled_data = next(d for d in disabled_docs if d and d["kind"] == "ConfigMap")["data"]
        self.assertEqual(disabled_data["MODEL_PIP_EXTRA_INDEX_URL"], "")
        self.assertEqual(disabled_data["KANIKO_CACHE"], "0")

        deployment = docs["Deployment"]["spec"]
        container = deployment["template"]["spec"]["containers"][0]
        self.assertEqual(container["image"], images["gateway"]["ref"])
        self.assertEqual(deployment["replicas"], 1)
        self.assertEqual(docs["Service"]["spec"]["ports"][0]["port"], 8000)
        self.assertEqual(docs["Service"]["spec"]["type"], "LoadBalancer")

    def test_config_checksum_changes_with_config(self) -> None:
        def checksum(config: dict[str, str]) -> str:
            return bentloy.gateway_values(config, bentloy.resolve_images(config))["config_checksum"]

        self.assertNotEqual(checksum(make_config()), checksum(make_config(KANIKO_SKIP_TLS_VERIFY="1")))

    def test_invalid_values_fail(self) -> None:
        for overrides in [{"K8S_NAMESPACE": "Bad_NS"}, {"GATEWAY_PORT": "0"},
                          {"GATEWAY_SERVICE_TYPE": "Ingress"}]:
            config = make_config(**overrides)
            with self.subTest(overrides=overrides), self.assertRaises(bentloy.CliError):
                bentloy.gateway_values(config, bentloy.resolve_images(config))

    def test_registry_secret(self) -> None:
        self.assertIsNone(bentloy.registry_secret_manifest(make_config()))

        config = make_config(REGISTRY_USERNAME="user", REGISTRY_PASSWORD="p@ss")
        secret = yaml.safe_load(bentloy.registry_secret_manifest(config))
        self.assertEqual(secret["type"], "kubernetes.io/dockerconfigjson")
        decoded = json.loads(base64.b64decode(secret["data"][".dockerconfigjson"]))
        auth = decoded["auths"]["registry.example.com"]
        self.assertEqual(base64.b64decode(auth["auth"]).decode(), "user:p@ss")

        redacted = bentloy.registry_secret_manifest(config, redact=True)
        self.assertNotIn("p@ss", redacted)


class PreflightTest(unittest.TestCase):
    def test_docker_engine_not_running(self) -> None:
        failure = mock.Mock(
            returncode=1, stdout="", stderr="failed to connect to the docker API at npipe\nmore"
        )
        with mock.patch.object(bentloy, "require_tool"), \
                mock.patch.object(bentloy.subprocess, "run", return_value=failure), \
                self.assertRaisesRegex(bentloy.CliError, r"engine is not running[\s\S]*npipe"):
            bentloy.require_docker_daemon()

    def test_cluster_unreachable(self) -> None:
        failure = mock.Mock(returncode=1, stdout="", stderr="Unable to connect to the server")
        with mock.patch.object(bentloy.subprocess, "run", return_value=failure), \
                self.assertRaisesRegex(bentloy.CliError, "Cannot reach the Kubernetes cluster"):
            bentloy.namespace_exists("ns")

    def test_namespace_not_found(self) -> None:
        missing = mock.Mock(
            returncode=1, stdout="", stderr='Error from server (NotFound): namespaces "ns" not found'
        )
        with mock.patch.object(bentloy.subprocess, "run", return_value=missing):
            self.assertFalse(bentloy.namespace_exists("ns"))

    def _run_up(self, config, build, namespace_exists, secret_exists) -> list[str]:
        applied: list[str] = []
        with mock.patch.object(bentloy, "require_tool"), \
                mock.patch.object(bentloy, "namespace_exists", return_value=namespace_exists), \
                mock.patch.object(bentloy, "secret_exists", return_value=secret_exists), \
                mock.patch.object(bentloy, "build_images", build), \
                mock.patch.object(bentloy, "run"), \
                mock.patch.object(bentloy, "kubectl_apply", applied.append), \
                contextlib.redirect_stdout(io.StringIO()):
            bentloy.cmd_up(bentloy.build_parser().parse_args(["up", "--no-wait"]), config)
        return applied

    def test_missing_secret_fails_before_building(self) -> None:
        for namespace_exists in (True, False):
            with self.subTest(namespace_exists=namespace_exists):
                build = mock.Mock()
                with self.assertRaisesRegex(bentloy.CliError, "bentloy-registry-creds"):
                    self._run_up(make_config(), build, namespace_exists, secret_exists=False)
                build.assert_not_called()

    def test_existing_secret_is_used_without_credentials(self) -> None:
        build = mock.Mock()
        applied = self._run_up(make_config(), build, namespace_exists=True, secret_exists=True)
        build.assert_called_once()
        self.assertEqual(len(applied), 1)
        self.assertIn("kind: Deployment", applied[0])

    def test_credentials_create_the_secret(self) -> None:
        config = make_config(REGISTRY_USERNAME="u", REGISTRY_PASSWORD="p")
        build = mock.Mock()
        applied = self._run_up(config, build, namespace_exists=False, secret_exists=False)
        build.assert_called_once()
        self.assertEqual(len(applied), 2)
        self.assertIn("kind: Secret", applied[0])


class PredictInputTest(unittest.TestCase):
    def test_key_value_pairs_are_typed(self) -> None:
        data = bentloy.parse_predict_input(["x1=1", "x2=2.5", "name=abc", "flag=true"], "")
        self.assertEqual(data, {"x1": 1, "x2": 2.5, "name": "abc", "flag": True})

    def test_json_object_and_list(self) -> None:
        self.assertEqual(bentloy.parse_predict_input(['{"x1": 1}'], ""), {"x1": 1})
        self.assertEqual(bentloy.parse_predict_input(["[1, 2, 3]"], ""), {"features": [1, 2, 3]})

    def test_file_input(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "in.json"
            path.write_text('{"x1": 5, "x2": 2}', encoding="utf-8")
            self.assertEqual(bentloy.parse_predict_input([], str(path)), {"x1": 5, "x2": 2})
            with self.assertRaises(bentloy.CliError):
                bentloy.parse_predict_input(["x1=1"], str(path))

    def test_invalid_inputs(self) -> None:
        for values in ([], ["x1"], ["=1"], ['{"x1": }'], ['"just a string"']):
            with self.subTest(values=values), self.assertRaises(bentloy.CliError):
                bentloy.parse_predict_input(values, "")

    def test_predict_command_calls_gateway(self) -> None:
        calls = []

        @contextlib.contextmanager
        def fake_connection(config, url):
            yield "http://gw"

        def fake_call(url, endpoint, payload):
            calls.append((endpoint, payload))
            return {"model": "m", "predictions": [{"y_predictions": 13.9}]}

        args = bentloy.build_parser().parse_args(["predict", "m", "x1=5", "x2=2", "x3=3"])
        with mock.patch.object(bentloy, "gateway_connection", fake_connection), \
                mock.patch.object(bentloy, "call_gateway", fake_call), \
                contextlib.redirect_stdout(io.StringIO()) as out:
            bentloy.cmd_predict(args, make_config())

        self.assertEqual(calls, [("predict", {"model_name": "m", "data": {"x1": 5, "x2": 2, "x3": 3}})])
        self.assertIn("13.9", out.getvalue())

    def test_predict_error_exits_nonzero(self) -> None:
        @contextlib.contextmanager
        def fake_connection(config, url):
            yield "http://gw"

        args = bentloy.build_parser().parse_args(["predict", "m", "[1, 2]"])
        with mock.patch.object(bentloy, "gateway_connection", fake_connection), \
                mock.patch.object(bentloy, "call_gateway", lambda *a: {"error": "expects 3 features"}), \
                contextlib.redirect_stdout(io.StringIO()), \
                self.assertRaisesRegex(bentloy.CliError, "expects 3 features"):
            bentloy.cmd_predict(args, make_config())


class CommandsTest(unittest.TestCase):
    def test_up_dry_run_runs_nothing(self) -> None:
        config = make_config(REGISTRY_USERNAME="user", REGISTRY_PASSWORD="secret-pw")
        args = bentloy.build_parser().parse_args(["up", "--dry-run"])
        out = io.StringIO()
        with mock.patch("subprocess.run") as run, mock.patch("subprocess.Popen") as popen, \
                contextlib.redirect_stdout(out):
            bentloy.cmd_up(args, config)
        run.assert_not_called()
        popen.assert_not_called()
        self.assertIn("kind: Deployment", out.getvalue())
        self.assertNotIn("secret-pw", out.getvalue())

    def test_build_print_only(self) -> None:
        args = bentloy.build_parser().parse_args(["build", "--print-only"])
        with mock.patch("subprocess.run") as run, contextlib.redirect_stdout(io.StringIO()) as out:
            bentloy.cmd_build(args, make_config())
        run.assert_not_called()
        self.assertRegex(out.getvalue(), r"GATEWAY_IMAGE=registry\.example\.com/my-org/model-gateway:")
        self.assertRegex(out.getvalue(), r"MODEL_BASE_IMAGE=registry\.example\.com/my-org/model-service-base:")

    def test_build_skips_existing_and_builds_missing(self) -> None:
        config = make_config()
        images = bentloy.resolve_images(config)
        existing = images["gateway"]["ref"]
        commands = []
        with mock.patch.object(bentloy, "require_docker_daemon"), \
                mock.patch.object(bentloy, "image_exists", lambda ref, insecure: ref == existing), \
                mock.patch.object(bentloy, "run", lambda cmd, **kw: commands.append(cmd)), \
                contextlib.redirect_stdout(io.StringIO()):
            bentloy.build_images(config, images, force=False)

        base = images["model-base"]["ref"]
        self.assertEqual(commands, [
            ["docker", "build", "-t", base, str(images["model-base"]["context"])],
            ["docker", "push", base],
        ])

    def test_delete_accepts_deployed_or_service_name(self) -> None:
        calls = []

        @contextlib.contextmanager
        def fake_connection(config, url):
            yield "http://gw"

        with mock.patch.object(bentloy, "gateway_connection", fake_connection), \
                mock.patch.object(bentloy, "call_gateway", lambda url, ep, payload: calls.append(payload)), \
                contextlib.redirect_stdout(io.StringIO()):
            for name in ["Tiny_Demo_v2", "bentloy-model-tiny-demo-v2"]:
                bentloy.cmd_delete(bentloy.build_parser().parse_args(["delete", name]), make_config())

        self.assertEqual(calls, [{"service_name": "bentloy-model-tiny-demo-v2"}] * 2)

    def test_deploy_waits_until_ready(self) -> None:
        responses = iter([
            {"operation_id": "op1", "deployed_name": "m", "image_ref": "img"},
            {"status": "building", "step": "create_job"},
            {"status": "ready", "step": "completed", "deployed_name": "m"},
        ])

        @contextlib.contextmanager
        def fake_connection(config, url):
            yield "http://gw"

        args = bentloy.build_parser().parse_args(["deploy", "m"])
        with mock.patch.object(bentloy, "gateway_connection", fake_connection), \
                mock.patch.object(bentloy, "call_gateway", lambda *a, **k: next(responses)), \
                mock.patch.object(bentloy.time, "sleep"), \
                contextlib.redirect_stdout(io.StringIO()) as out:
            bentloy.cmd_deploy(args, make_config())
        self.assertIn('model_name="m"', out.getvalue())

    def test_deploy_failure_raises(self) -> None:
        responses = iter([
            {"operation_id": "op1", "deployed_name": "m", "image_ref": "img"},
            {"status": "failed", "step": "failed", "error": "boom"},
        ])

        @contextlib.contextmanager
        def fake_connection(config, url):
            yield "http://gw"

        args = bentloy.build_parser().parse_args(["deploy", "m"])
        with mock.patch.object(bentloy, "gateway_connection", fake_connection), \
                mock.patch.object(bentloy, "call_gateway", lambda *a, **k: next(responses)), \
                contextlib.redirect_stdout(io.StringIO()), \
                self.assertRaisesRegex(bentloy.CliError, "boom"):
            bentloy.cmd_deploy(args, make_config())

    def test_deploy_and_models_send_experiment_and_run(self) -> None:
        calls = []

        def fake_call(url, endpoint, payload, retries=0):
            calls.append((endpoint, payload))
            if endpoint == "deploy_model":
                return {"operation_id": "op1", "deployed_name": "m", "image_ref": "img"}
            return {"endpoints": {}}

        @contextlib.contextmanager
        def fake_connection(config, url):
            yield "http://gw"

        parser = bentloy.build_parser()
        run_id = "0123456789abcdef0123456789abcdef"
        with mock.patch.object(bentloy, "gateway_connection", fake_connection),                 mock.patch.object(bentloy, "call_gateway", fake_call),                 contextlib.redirect_stdout(io.StringIO()):
            bentloy.cmd_deploy(parser.parse_args([
                "deploy", "m", "--run-id", run_id, "--model-uri", f"runs:/{run_id}/ludwig-model",
                "--experiment-name", "Churn EU", "--no-wait",
            ]), make_config())
            bentloy.cmd_models(parser.parse_args([
                "models", "--experiment-name", "Churn EU", "--run-id", run_id,
            ]), make_config())

        deploy_payload = calls[0][1]
        self.assertEqual(deploy_payload["run_id"], run_id)
        self.assertEqual(deploy_payload["model_uri"], f"runs:/{run_id}/ludwig-model")
        self.assertEqual(deploy_payload["experiment_name"], "Churn EU")
        self.assertEqual(calls[1][0], "list_models")
        self.assertEqual(calls[1][1]["experiment_name"], "Churn EU")
        self.assertEqual(calls[1][1]["run_id"], run_id)


if __name__ == "__main__":
    unittest.main()
