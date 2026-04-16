import asyncio
import re
import uuid
from datetime import datetime, timezone
from typing import Any, Callable

from ..domain.helpers import (
    is_valid_image_ref,
    is_valid_k8s_name,
    is_valid_registry_server,
    to_model_slug,
)
from ..domain.types import DeployOperationRecord, DeployOperationRequest
from .operation_store import DeployOperationStore
from .orchestrator import DeployOrchestrator


class DeployHandler:
    """Public deploy API logic for enqueueing and reporting operations."""

    def __init__(
        self,
        *,
        auto_deploy_enabled: bool,
        model_namespace: str,
        default_tracking_uri: str,
        kaniko_skip_tls_verify_default: bool,
        registry_server: str,
        registry_org: str,
        model_base_image: str,
        registry_secret: str,
        operation_store: DeployOperationStore,
        deploy_orchestrator: DeployOrchestrator,
        now_iso: Callable[[], str],
    ):
        self.auto_deploy_enabled = auto_deploy_enabled
        self.model_namespace = model_namespace
        self.default_tracking_uri = default_tracking_uri
        self.kaniko_skip_tls_verify_default = kaniko_skip_tls_verify_default
        self.registry_server = registry_server
        self.registry_org = registry_org
        self.model_base_image = model_base_image
        self.registry_secret = registry_secret
        self.operation_store = operation_store
        self.deploy_orchestrator = deploy_orchestrator
        self.now_iso = now_iso

    def _build_operation_id(self) -> str:
        timestamp = datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S")
        suffix = uuid.uuid4().hex[:6]
        return f"op_{timestamp}_{suffix}"

    def _build_target_image(
        self,
        model_slug: str,
        model_version: str,
        run_id: str,
        registry_server: str,
        org: str,
    ) -> str:
        model_version_slug = re.sub(r"[^a-z0-9._-]+", "-", model_version.lower()).strip("-")
        if not model_version_slug:
            model_version_slug = "latest"

        if run_id:
            tag = f"v{model_version_slug}-r{run_id[:8]}"
        else:
            now_utc = datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S")
            tag = f"v{model_version_slug}-t{now_utc}"

        return f"{registry_server}/{org}/model-{model_slug}:{tag}"

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
    ) -> dict[str, Any]:
        if not self.auto_deploy_enabled:
            return {"error": "Automatic deploy is disabled on this gateway."}

        namespace = namespace.strip() or self.model_namespace
        model_slug = to_model_slug(model_name)
        deployed_name = (deployed_name or "").strip() or model_name
        append_date = bool(append_date)
        if append_date:
            deployed_name = f"{deployed_name}_{datetime.now(timezone.utc).strftime('%Y%m%d')}"
        deployment_slug = to_model_slug(deployed_name)
        model_version = (model_version or "latest").strip()
        run_id = run_id.strip()
        tracking_uri = tracking_uri.strip() or self.default_tracking_uri
        model_uri = model_uri.strip()
        target_image = target_image.strip()
        insecure_registry_tls = (
            bool(insecure_registry_tls) or self.kaniko_skip_tls_verify_default
        )

        if not model_slug:
            return {"error": "Invalid model_name. Could not generate slug."}
        if not deployment_slug:
            return {"error": "Invalid deployed_name. Could not generate slug."}
        if not is_valid_k8s_name(namespace):
            return {"error": f"Invalid namespace '{namespace}'."}
        if replicas < 1:
            return {"error": "replicas must be >= 1."}
        if run_id and not re.fullmatch(r"[A-Za-z0-9._-]{4,128}", run_id):
            return {"error": "Invalid run_id format."}
        if not tracking_uri and not model_uri:
            return {"error": "Set tracking_uri or model_uri."}

        registry_server = self.registry_server
        registry_org = self.registry_org
        if not target_image:
            if not registry_server or not registry_org:
                return {
                    "error": "Missing REGISTRY_SERVER/TU_ORG or pass target_image explicitly."
                }
            if not is_valid_registry_server(registry_server):
                return {"error": f"Invalid REGISTRY_SERVER '{registry_server}'."}
            target_image = self._build_target_image(
                model_slug=model_slug,
                model_version=model_version,
                run_id=run_id,
                registry_server=registry_server,
                org=registry_org,
            )

        if not is_valid_image_ref(target_image):
            return {"error": f"Invalid target_image '{target_image}'."}

        if self.model_base_image:
            base_image = self.model_base_image
        else:
            if not registry_server or not registry_org:
                return {"error": "Missing MODEL_BASE_IMAGE and REGISTRY_SERVER/TU_ORG."}
            base_image = f"{registry_server}/{registry_org}/model-service-base:py310"

        if not is_valid_image_ref(base_image):
            return {"error": f"Invalid base image '{base_image}'."}

        operation_id = self._build_operation_id()
        job_name = f"build-model-{deployment_slug}-{operation_id[-6:]}"
        job_name = re.sub(r"[^a-z0-9-]+", "-", job_name.lower()).strip("-")[:63].rstrip("-")

        request: DeployOperationRequest = {
            "namespace": namespace,
            "model_name": model_name,
            "model_slug": model_slug,
            "deployed_name": deployed_name,
            "deployment_slug": deployment_slug,
            "model_version": model_version,
            "model_uri": model_uri,
            "tracking_uri": tracking_uri,
            "replicas": replicas,
            "target_image": target_image,
            "base_image": base_image,
            "registry_secret": self.registry_secret,
            "insecure_registry_tls": insecure_registry_tls,
            "registry_server": registry_server,
            "job_name": job_name,
        }

        operation: DeployOperationRecord = {
            "operation_id": operation_id,
            "status": "queued",
            "step": "queued",
            "created_at": self.now_iso(),
            "updated_at": self.now_iso(),
            "model_name": model_name,
            "deployed_name": deployed_name,
            "model_version": model_version,
            "namespace": namespace,
            "replicas": replicas,
            "image_ref": target_image,
            "k8s_job": job_name,
            "error": None,
            "message": None,
            "request": request,
        }

        await self.operation_store.add(operation)
        asyncio.create_task(self.deploy_orchestrator.run_operation(operation_id))

        return {
            "operation_id": operation_id,
            "status": "queued",
            "model_name": model_name,
            "deployed_name": deployed_name,
            "namespace": namespace,
            "image_ref": target_image,
            "k8s_job": job_name,
        }

    async def deploy_status(self, operation_id: str) -> dict[str, Any]:
        operation = await self.operation_store.get(operation_id)
        if not operation:
            return {"error": "operation_id not found", "operation_id": operation_id}

        return {
            "operation_id": operation["operation_id"],
            "status": operation["status"],
            "step": operation["step"],
            "model_name": operation["model_name"],
            "deployed_name": operation["deployed_name"],
            "model_version": operation["model_version"],
            "namespace": operation["namespace"],
            "image_ref": operation["image_ref"],
            "k8s_job": operation["k8s_job"],
            "error": operation["error"],
            "message": operation["message"],
            "created_at": operation["created_at"],
            "updated_at": operation["updated_at"],
        }
