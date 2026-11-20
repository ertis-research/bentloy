"""Environment-based settings loading for the gateway service."""

import os
from dataclasses import dataclass
from typing import Mapping

from ..domain.helpers import as_bool, normalize_registry_server, positive_int_from_env

# PyTorch CPU wheels: models depending on torch (e.g. Ludwig) skip the CUDA libraries.
DEFAULT_MODEL_PIP_EXTRA_INDEX_URL = "https://download.pytorch.org/whl/cpu"


@dataclass(frozen=True)
class GatewaySettings:
    """Runtime configuration required by gateway routing and deploy features."""

    model_urls: dict[str, str]
    model_service_prefix: str
    model_namespace: str
    model_url_template: str
    auto_deploy_enabled: bool
    registry_server: str
    registry_namespace: str
    model_base_image: str
    registry_secret: str
    kaniko_image: str
    kaniko_skip_tls_verify_default: bool
    kaniko_cache: bool
    model_pip_extra_index_url: str
    default_tracking_uri: str
    build_timeout_sec: int
    rollout_timeout_sec: int

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> "GatewaySettings":
        """Build settings from environment variables.

        The loader scans `MODELO_*_URL` variables and converts them into
        model-name-to-base-url mappings used by the prediction router.
        """
        source = env if env is not None else os.environ

        model_urls: dict[str, str] = {}
        for key, value in source.items():
            if key.endswith("_URL") and key.startswith("MODELO_"):
                model_name = key.replace("MODELO_", "").replace("_URL", "").lower()
                model_urls[model_name] = value

        return cls(
            model_urls=model_urls,
            model_service_prefix=source.get("MODEL_SERVICE_PREFIX", "bentloy-model"),
            model_namespace=source.get("K8S_NAMESPACE", "").strip() or "default",
            model_url_template=source.get(
                "MODEL_URL_TEMPLATE",
                "http://{service_name}:3000",
            ),
            auto_deploy_enabled=as_bool(source.get("AUTO_DEPLOY_ENABLED"), default=True),
            registry_server=normalize_registry_server(source.get("REGISTRY_SERVER", "")),
            registry_namespace=source.get("REGISTRY_NAMESPACE", "").strip(),
            model_base_image=source.get("MODEL_BASE_IMAGE", "").strip(),
            registry_secret=source.get(
                "MODEL_REGISTRY_SECRET",
                "bentloy-registry-creds",
            ).strip(),
            kaniko_image=source.get(
                "KANIKO_IMAGE",
                "gcr.io/kaniko-project/executor:v1.23.2-debug",
            ).strip(),
            kaniko_skip_tls_verify_default=as_bool(
                source.get("KANIKO_SKIP_TLS_VERIFY"),
                default=False,
            ),
            kaniko_cache=as_bool(source.get("KANIKO_CACHE"), default=True),
            # Set to an empty value to disable (e.g. GPU clusters that need CUDA wheels).
            model_pip_extra_index_url=source.get(
                "MODEL_PIP_EXTRA_INDEX_URL",
                DEFAULT_MODEL_PIP_EXTRA_INDEX_URL,
            ).strip(),
            default_tracking_uri=source.get("MLFLOW_TRACKING_URI", "").strip(),
            build_timeout_sec=positive_int_from_env(
                source,
                "DEPLOY_BUILD_TIMEOUT_SEC",
                1800,
            ),
            rollout_timeout_sec=positive_int_from_env(
                source,
                "DEPLOY_ROLLOUT_TIMEOUT_SEC",
                300,
            ),
        )
