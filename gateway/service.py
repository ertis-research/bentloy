"""Gateway Bento service entrypoint and API surface."""

import functools
import logging
from datetime import datetime, timezone
from typing import Any

import bentoml

try:
    # Package imports (tests/local tooling)
    from .app.config.settings import GatewaySettings
    from .app.deploy.deploy_handler import DeployHandler, DeployRequestError
    from .app.deploy.operation_store import DeployOperationStore
    from .app.deploy.orchestrator import DeployOrchestrator
    from .app.handlers.info_handler import InfoHandler, ListModelsRequestError
    from .app.handlers.predict_handler import PredictHandler
    from .app.kubernetes.auth import resolve_k8s_auth
    from .app.kubernetes.client import KubernetesApiClient
    from .app.manifests.deployment_manifest import build_deployment_manifest
    from .app.manifests.job_manifest import build_job_manifest
    from .app.manifests.job_manifest import build_delete_job_manifest
    from .app.manifests.service_manifest import build_service_manifest
    from .app.routing.discovery import KubernetesModelDiscovery
    from .app.routing.target_resolver import PredictTargetResolver
except ImportError:
    # Bento runtime imports when /app is working directory in container
    from app.config.settings import GatewaySettings
    from app.deploy.deploy_handler import DeployHandler, DeployRequestError
    from app.deploy.operation_store import DeployOperationStore
    from app.deploy.orchestrator import DeployOrchestrator
    from app.handlers.info_handler import InfoHandler, ListModelsRequestError
    from app.handlers.predict_handler import PredictHandler
    from app.kubernetes.auth import resolve_k8s_auth
    from app.kubernetes.client import KubernetesApiClient
    from app.manifests.deployment_manifest import build_deployment_manifest
    from app.manifests.job_manifest import build_job_manifest
    from app.manifests.job_manifest import build_delete_job_manifest
    from app.manifests.service_manifest import build_service_manifest
    from app.routing.discovery import KubernetesModelDiscovery
    from app.routing.target_resolver import PredictTargetResolver


# Child of the "bentoml" logger so BentoML's handlers and INFO level apply in the running server.
logger = logging.getLogger("bentoml.gateway")


@bentoml.service(resources={"cpu": "1"}, traffic={"timeout": 60})
class GatewayService:
    """API gateway that routes predictions to multiple models."""

    def __init__(self):
        """Initialize runtime settings, dependencies, and request handlers."""

        settings = GatewaySettings.from_env()
        self.model_urls = settings.model_urls
        self.model_service_prefix = settings.model_service_prefix
        self.model_namespace = settings.model_namespace
        self.model_url_template = settings.model_url_template
        self.auto_deploy_enabled = settings.auto_deploy_enabled
        self.registry_server = settings.registry_server
        self.registry_namespace = settings.registry_namespace
        self.model_base_image = settings.model_base_image
        self.registry_secret = settings.registry_secret
        self.kaniko_image = settings.kaniko_image
        self.kaniko_skip_tls_verify_default = settings.kaniko_skip_tls_verify_default
        self.default_tracking_uri = settings.default_tracking_uri
        self.build_timeout_sec = settings.build_timeout_sec
        self.rollout_timeout_sec = settings.rollout_timeout_sec

        self.target_resolver = PredictTargetResolver(
            model_urls=self.model_urls,
            model_service_prefix=self.model_service_prefix,
            model_namespace=self.model_namespace,
            model_url_template=self.model_url_template,
        )
        self.k8s_client = KubernetesApiClient()
        self.model_discovery = KubernetesModelDiscovery(
            model_service_prefix=self.model_service_prefix,
            model_namespace=self.model_namespace,
            model_url_template=self.model_url_template,
        )
        self.operation_store = DeployOperationStore(now_iso=self._now_iso)
        self.deploy_orchestrator = DeployOrchestrator(
            operation_store=self.operation_store,
            k8s_client=self.k8s_client,
            resolve_k8s_auth=self._resolve_k8s_auth,
            build_job_manifest=functools.partial(
                build_job_manifest,
                pip_extra_index_url=settings.model_pip_extra_index_url,
                kaniko_cache=settings.kaniko_cache,
            ),
            build_delete_job_manifest=build_delete_job_manifest,
            build_deployment_manifest=build_deployment_manifest,
            build_service_manifest=build_service_manifest,
            kaniko_image=self.kaniko_image,
            model_service_prefix=self.model_service_prefix,
            build_timeout_sec=self.build_timeout_sec,
            rollout_timeout_sec=self.rollout_timeout_sec,
            registry_secret=self.registry_secret,
            insecure_registry_tls_default=self.kaniko_skip_tls_verify_default,
        )

        # Stores models observed in predict/batch_predict calls.
        self.recently_routed_models: dict[str, str] = {}

        self.deploy_handler = DeployHandler(
            auto_deploy_enabled=self.auto_deploy_enabled,
            model_namespace=self.model_namespace,
            default_tracking_uri=self.default_tracking_uri,
            kaniko_skip_tls_verify_default=self.kaniko_skip_tls_verify_default,
            registry_server=self.registry_server,
            registry_namespace=self.registry_namespace,
            model_base_image=self.model_base_image,
            registry_secret=self.registry_secret,
            operation_store=self.operation_store,
            deploy_orchestrator=self.deploy_orchestrator,
            now_iso=self._now_iso,
            model_service_prefix=self.model_service_prefix,
        )
        self.predict_handler = PredictHandler(
            target_resolver=self.target_resolver,
            model_urls=self.model_urls,
            recently_routed_models=self.recently_routed_models,
        )
        self.info_handler = InfoHandler(
            model_urls=self.model_urls,
            model_service_prefix=self.model_service_prefix,
            model_namespace=self.model_namespace,
            model_url_template=self.model_url_template,
            recently_routed_models=self.recently_routed_models,
            model_discovery=self.model_discovery,
            operation_store=self.operation_store,
            resolve_k8s_auth=self._resolve_k8s_auth,
        )

        logger.info("Configured models from env: %s", list(self.model_urls.keys()))
        logger.info(
            "Auto deploy enabled: %s | registry_server=%s",
            self.auto_deploy_enabled,
            self.registry_server or "<unset>",
        )

    def _now_iso(self) -> str:
        """Return the current UTC timestamp in ISO-8601 format."""

        return datetime.now(timezone.utc).isoformat()

    def _resolve_k8s_auth(self) -> tuple[str, dict[str, str], str | bool]:
        """Resolve in-cluster Kubernetes API authentication details."""

        return resolve_k8s_auth()

    async def _discover_k8s_model_services(self) -> tuple[list[dict[str, str]], str | None]:
        """Discover model services from Kubernetes for dynamic routing metadata."""

        return await self.info_handler.discover_k8s_model_services()

    @bentoml.api
    async def deploy_model(
        self,
        model_name: str,
        model_version: str = "latest",
        namespace: str = "",
        replicas: int = 1,
        run_id: str = "",
        tracking_uri: str = "",
        model_uri: str = "",
        target_image: str = "",
        insecure_registry_tls: bool = False,
        deployed_name: str = "",
        append_date: bool = False,
        experiment_name: str = "",
        ctx: bentoml.Context = None,
    ) -> dict[str, Any]:
        """Queue a model deployment operation managed by the gateway.

        `model_name` names the deployment; `experiment_name` is the MLflow experiment.
        Invalid requests answer with a 4xx (5xx for gateway misconfiguration) and
        {"error": msg, "detail": msg}.
        """

        try:
            return await self.deploy_handler.deploy_model(
                model_name=model_name,
                model_version=model_version,
                namespace=namespace,
                replicas=replicas,
                run_id=run_id,
                tracking_uri=tracking_uri,
                model_uri=model_uri,
                target_image=target_image,
                insecure_registry_tls=insecure_registry_tls,
                deployed_name=deployed_name,
                append_date=append_date,
                experiment_name=experiment_name,
            )
        except DeployRequestError as exc:
            if ctx is not None:
                ctx.response.status_code = exc.status_code
            return {"error": exc.message, "detail": exc.message}

    @bentoml.api
    async def deploy_status(self, operation_id: str) -> dict[str, Any]:
        """Return status details for a previously queued deploy operation."""

        return await self.deploy_handler.deploy_status(operation_id=operation_id)

    @bentoml.api
    async def predict(
        self,
        model_name: str,
        data: dict[str, Any],
        model_url: str = "",
    ) -> dict[str, Any]:
        """Forward one prediction request to the resolved model endpoint."""

        return await self.predict_handler.predict(
            model_name=model_name,
            data=data,
            model_url=model_url,
        )

    @bentoml.api
    async def batch_predict(self, requests: list[dict]) -> dict[str, Any]:
        """Forward a batch of prediction requests and aggregate responses."""

        return await self.predict_handler.batch_predict(requests=requests)

    @bentoml.api
    async def health(self) -> dict[str, Any]:
        """Return gateway and configured model health information."""

        return await self.info_handler.health()

    @bentoml.api
    async def list_models(
        self,
        page: int = 1,
        page_size: int = 10,
        query: str = "",
        experiment_name: str = "",
        run_id: str = "",
        ctx: bentoml.Context = None,
    ) -> dict[str, Any]:
        """List configured, discovered, and recently routed models.

        `page` is 1-indexed (default 1); `page < 1` answers 400 with
        {"error": msg, "detail": msg}.
        `experiment_name` / `run_id` filter discovered models and deploy operations exactly.
        """

        try:
            return await self.info_handler.list_models(
                page, page_size, query, experiment_name=experiment_name, run_id=run_id
            )
        except ListModelsRequestError as exc:
            if ctx is not None:
                ctx.response.status_code = exc.status_code
            return {"error": exc.message, "detail": exc.message}

    @bentoml.api
    async def delete_operation(self, operation_id: str):
        """Delete oepration from system"""
        await self.deploy_handler.delete_operation(operation_id)

    @bentoml.api
    async def delete_model(self, service_name: str):
        """Delete model from system"""
        await self.deploy_handler.delete_model(service_name)
    
