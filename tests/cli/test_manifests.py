"""Tests for the manifests rendered by the CLI."""

import base64
import json
from collections.abc import Callable

import pytest
import yaml

import bentloy


def test_gateway_manifest_renders_valid_yaml(make_config: Callable[..., dict[str, str]]) -> None:
    """The gateway manifest is valid YAML with the expected resources and values."""
    config = make_config(MLFLOW_TRACKING_URI='http://h:5000/?a=1&b="2"')
    images = bentloy.resolve_images(config)
    manifest = bentloy.render("gateway.yaml", bentloy.gateway_values(config, images))
    docs = {doc["kind"]: doc for doc in yaml.safe_load_all(manifest) if doc}

    assert set(docs) == {"ServiceAccount", "Role", "RoleBinding", "ConfigMap", "Deployment", "Service"}
    for doc in docs.values():
        assert doc["metadata"]["namespace"] == "api-models"
        # Everything Bentloy creates is prefixed with "bentloy-".
        assert doc["metadata"]["name"].startswith("bentloy-"), doc["metadata"]["name"]
    assert docs["Service"]["metadata"]["name"] == "bentloy-gateway"
    assert docs["Deployment"]["spec"]["template"]["spec"]["serviceAccountName"] == "bentloy-gateway-sa"
    assert docs["RoleBinding"]["roleRef"]["name"] == docs["Role"]["metadata"]["name"]
    assert docs["ConfigMap"]["data"]["MODEL_SERVICE_PREFIX"] == "bentloy-model"
    data = docs["ConfigMap"]["data"]
    assert data["K8S_NAMESPACE"] == "api-models"
    assert data["MLFLOW_TRACKING_URI"] == 'http://h:5000/?a=1&b="2"'
    assert data["REGISTRY_SERVER"] == "registry.example.com"
    assert data["MODEL_BASE_IMAGE"] == images["model-base"]["ref"]
    assert all(isinstance(value, str) for value in data.values())
    assert data["KANIKO_CACHE"] == "1"
    assert data["MODEL_PIP_EXTRA_INDEX_URL"] == "https://download.pytorch.org/whl/cpu"
    disabled = make_config(MODEL_PIP_EXTRA_INDEX_URL="", KANIKO_CACHE="0")
    disabled_docs = yaml.safe_load_all(
        bentloy.render("gateway.yaml", bentloy.gateway_values(disabled, images))
    )
    disabled_data = next(d for d in disabled_docs if d and d["kind"] == "ConfigMap")["data"]
    assert disabled_data["MODEL_PIP_EXTRA_INDEX_URL"] == ""
    assert disabled_data["KANIKO_CACHE"] == "0"

    deployment = docs["Deployment"]["spec"]
    container = deployment["template"]["spec"]["containers"][0]
    assert container["image"] == images["gateway"]["ref"]
    assert deployment["replicas"] == 1
    assert docs["Service"]["spec"]["ports"][0]["port"] == 8000
    assert docs["Service"]["spec"]["type"] == "LoadBalancer"


def test_config_checksum_changes_with_config(make_config: Callable[..., dict[str, str]]) -> None:
    """The config checksum changes when a ConfigMap value changes."""

    def checksum(config: dict[str, str]) -> str:
        """Return the config checksum rendered for ``config``."""
        return bentloy.gateway_values(config, bentloy.resolve_images(config))["config_checksum"]

    assert checksum(make_config()) != checksum(make_config(KANIKO_SKIP_TLS_VERIFY="1"))


@pytest.mark.parametrize(
    "overrides",
    [
        pytest.param({"K8S_NAMESPACE": "Bad_NS"}, id="bad-namespace"),
        pytest.param({"GATEWAY_PORT": "0"}, id="bad-port"),
        pytest.param({"GATEWAY_SERVICE_TYPE": "Ingress"}, id="bad-service-type"),
    ],
)
def test_invalid_values_fail(make_config: Callable[..., dict[str, str]], overrides: dict[str, str]) -> None:
    """Invalid namespace, port or service type values are rejected."""
    config = make_config(**overrides)
    with pytest.raises(bentloy.CliError):
        bentloy.gateway_values(config, bentloy.resolve_images(config))


def test_registry_secret(make_config: Callable[..., dict[str, str]]) -> None:
    """The registry secret is only rendered with credentials, and can be redacted."""
    assert bentloy.registry_secret_manifest(make_config()) is None

    config = make_config(REGISTRY_USERNAME="user", REGISTRY_PASSWORD="p@ss")
    secret = yaml.safe_load(bentloy.registry_secret_manifest(config))
    assert secret["type"] == "kubernetes.io/dockerconfigjson"
    decoded = json.loads(base64.b64decode(secret["data"][".dockerconfigjson"]))
    auth = decoded["auths"]["registry.example.com"]
    assert base64.b64decode(auth["auth"]).decode() == "user:p@ss"

    redacted = bentloy.registry_secret_manifest(config, redact=True)
    assert "p@ss" not in redacted
