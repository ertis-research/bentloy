"""Tests for GatewaySettings."""

from gateway.app.config.settings import GatewaySettings


def test_defaults() -> None:
    """An empty environment yields the documented defaults."""
    settings = GatewaySettings.from_env({})
    assert settings.model_service_prefix == "bentloy-model"
    assert settings.model_namespace == "default"
    assert settings.model_url_template == "http://{service_name}:3000"
    assert settings.auto_deploy_enabled
    assert settings.registry_server == ""
    assert settings.registry_secret == "bentloy-registry-creds"
    assert settings.build_timeout_sec == 1800
    assert settings.rollout_timeout_sec == 300
    assert settings.model_urls == {}
    assert settings.kaniko_cache
    assert settings.model_pip_extra_index_url == "https://download.pytorch.org/whl/cpu"


def test_build_options_can_be_disabled() -> None:
    """The Kaniko cache and the extra pip index can be turned off."""
    settings = GatewaySettings.from_env({"KANIKO_CACHE": "0", "MODEL_PIP_EXTRA_INDEX_URL": ""})
    assert not settings.kaniko_cache
    assert settings.model_pip_extra_index_url == ""


def test_custom_values_and_model_urls() -> None:
    """Environment values override the defaults and MODELO_*_URL entries become model URLs."""
    env = {
        "MODEL_SERVICE_PREFIX": "ms",
        "K8S_NAMESPACE": "api-models",
        "MODEL_URL_TEMPLATE": "http://{service_name}.{namespace}:3000",
        "AUTO_DEPLOY_ENABLED": "false",
        "REGISTRY_SERVER": "https://registry.example.com/projects",
        "REGISTRY_NAMESPACE": "api-models",
        "MODEL_BASE_IMAGE": "registry.example.com/api-models/model-service-base:py310",
        "MODEL_REGISTRY_SECRET": "model-registry-creds",
        "KANIKO_IMAGE": "gcr.io/kaniko-project/executor:v1.23.2-debug",
        "KANIKO_SKIP_TLS_VERIFY": "1",
        "MLFLOW_TRACKING_URI": "http://mlflow.example.com:5000",
        "DEPLOY_BUILD_TIMEOUT_SEC": "2400",
        "DEPLOY_ROLLOUT_TIMEOUT_SEC": "450",
        "MODELO_TINY_DEMO_MODEL_URL": "http://model-tiny-demo-model:3000",
    }

    settings = GatewaySettings.from_env(env)

    assert settings.model_service_prefix == "ms"
    assert settings.model_namespace == "api-models"
    assert settings.model_url_template == "http://{service_name}.{namespace}:3000"
    assert not settings.auto_deploy_enabled
    assert settings.registry_server == "registry.example.com"
    assert settings.registry_namespace == "api-models"
    assert settings.kaniko_skip_tls_verify_default
    assert settings.default_tracking_uri == "http://mlflow.example.com:5000"
    assert settings.build_timeout_sec == 2400
    assert settings.rollout_timeout_sec == 450
    assert settings.model_urls == {"tiny_demo_model": "http://model-tiny-demo-model:3000"}
