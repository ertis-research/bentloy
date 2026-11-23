"""Gateway deploy API logic for validation, queueing, and status retrieval."""

import asyncio
import re
import uuid
from datetime import datetime, timezone
from typing import Any, Callable
import traceback
import httpx
from urllib.parse import quote_plus

from ..domain.helpers import (
    image_repository,
    is_valid_image_ref,
    is_valid_k8s_name,
    is_valid_label_value,
    is_valid_registry_server,
    to_model_slug,
)
from ..domain.k8s_labels import EXPERIMENT_LABEL, EXPERIMENT_NAME_ANNOTATION, RUN_ID_LABEL
from ..domain.types import DeployOperationRecord, DeployOperationRequest
from .errors import MAX_ERROR_CHARS, truncate
from .operation_store import DeployOperationStore
from .orchestrator import DeployOrchestrator


# Values of the `conflict` field in /deploy_model 409 answers.
CONFLICT_EXISTS = "exists"
CONFLICT_IN_PROGRESS = "in_progress"

K8S_NAME_MAX_LENGTH = 63


class DeployRequestError(Exception):
    """A deploy request that cannot be queued.

    Carries the HTTP status to answer with and optional `extra` fields that the API
    adds to `{"error", "detail"}` so clients can tell cases apart without parsing text.
    """

    def __init__(
        self, message: str, status_code: int = 400, extra: dict[str, Any] | None = None
    ):
        super().__init__(message)
        self.message = message
        self.status_code = status_code
        self.extra = dict(extra or {})


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
        registry_namespace: str,
        model_base_image: str,
        registry_secret: str,
        operation_store: DeployOperationStore,
        deploy_orchestrator: DeployOrchestrator,
        now_iso: Callable[[], str],
        model_service_prefix: str = "bentloy-model",
    ):
        self.auto_deploy_enabled = auto_deploy_enabled
        self.model_namespace = model_namespace
        self.default_tracking_uri = default_tracking_uri
        self.kaniko_skip_tls_verify_default = kaniko_skip_tls_verify_default
        self.registry_server = registry_server
        self.registry_namespace = registry_namespace
        self.model_base_image = model_base_image
        self.registry_secret = registry_secret
        self.operation_store = operation_store
        self.deploy_orchestrator = deploy_orchestrator
        self.now_iso = now_iso
        self.model_service_prefix = model_service_prefix

    def _build_operation_id(self) -> str:
        """Generate a sortable operation id with timestamp and random suffix."""

        timestamp = datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S")
        suffix = uuid.uuid4().hex[:6]
        return f"op_{timestamp}_{suffix}"

    def _build_target_image(
        self,
        model_slug: str,
        model_version: str,
        run_id: str,
        registry_server: str,
        namespace: str,
    ) -> str:
        """Build a deterministic target image reference for the model artifact."""

        model_version_slug = re.sub(r"[^a-z0-9._-]+", "-", model_version.lower()).strip("-")
        if not model_version_slug:
            model_version_slug = "latest"

        if run_id:
            tag = f"v{model_version_slug}-r{run_id[:8]}"
        else:
            now_utc = datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S")
            tag = f"v{model_version_slug}-t{now_utc}"

        return f"{image_repository(registry_server, namespace, f'model-{model_slug}')}:{tag}"

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
        overwrite: bool = False,
    ) -> dict[str, Any]:
        """Validate input, enqueue a deploy operation, and trigger async execution.

        `model_name` is the base name of the deployment (slug, image, default
        `deployed_name`); `experiment_name` is the MLflow experiment the run belongs to.
        Unless `overwrite` is true, a deployed_name whose resource already exists answers
        409; a deploy still in progress for that resource answers 409 even with overwrite.
        Raises DeployRequestError when the request cannot be queued.
        """

        if not self.auto_deploy_enabled:
            raise DeployRequestError("Automatic deploy is disabled on this gateway.", 403)

        namespace = namespace.strip() or self.model_namespace
        model_slug = to_model_slug(model_name)
        deployed_name = (deployed_name or "").strip() or model_name
        append_date = bool(append_date)
        if append_date:
            deployed_name = f"{deployed_name}_{datetime.now(timezone.utc).strftime('%Y%m%d')}"
        deployment_slug = to_model_slug(deployed_name)
        model_version = (model_version or "latest").strip()
        run_id = (run_id or "").strip()
        experiment_name = (experiment_name or "").strip()
        tracking_uri = tracking_uri.strip() or self.default_tracking_uri
        model_uri = model_uri.strip()
        target_image = target_image.strip()
        insecure_registry_tls = (
            bool(insecure_registry_tls) or self.kaniko_skip_tls_verify_default
        )

        if not model_slug:
            raise DeployRequestError("Invalid model_name. Could not generate slug.")
        # Longest slug that keeps `<prefix>-<slug>` a valid Kubernetes name.
        max_slug_length = K8S_NAME_MAX_LENGTH - len(self.model_service_prefix) - 1
        invalid_name = {"invalid_field": "deployed_name", "max_length": max_slug_length}
        if not deployment_slug:
            raise DeployRequestError(
                "Invalid deployed_name. Could not generate slug.", extra=invalid_name
            )
        resource_name = f"{self.model_service_prefix}-{deployment_slug}"
        if len(resource_name) > K8S_NAME_MAX_LENGTH:
            raise DeployRequestError(
                f"deployed_name too long: Kubernetes resource '{resource_name}' would exceed "
                f"{K8S_NAME_MAX_LENGTH} characters (max {max_slug_length} for the name).",
                extra=invalid_name,
            )
        if not is_valid_k8s_name(namespace):
            raise DeployRequestError(f"Invalid namespace '{namespace}'.")
        if replicas < 1:
            raise DeployRequestError("replicas must be >= 1.")
        if run_id and (len(run_id) < 4 or not is_valid_label_value(run_id)):
            raise DeployRequestError(
                "Invalid run_id format (4-63 chars: letters, digits, '.', '_', '-'; "
                "must start and end with a letter or digit)."
            )
        if not tracking_uri and not model_uri:
            raise DeployRequestError("Set tracking_uri or model_uri.")

        registry_server = self.registry_server
        registry_namespace = self.registry_namespace
        if not target_image:
            if not registry_server:
                raise DeployRequestError(
                    "Missing REGISTRY_SERVER or pass target_image explicitly.", 500
                )
            if not is_valid_registry_server(registry_server):
                raise DeployRequestError(f"Invalid REGISTRY_SERVER '{registry_server}'.", 500)
            target_image = self._build_target_image(
                model_slug=model_slug,
                model_version=model_version,
                run_id=run_id,
                registry_server=registry_server,
                namespace=registry_namespace,
            )

        if not is_valid_image_ref(target_image):
            raise DeployRequestError(f"Invalid target_image '{target_image}'.")

        if self.model_base_image:
            base_image = self.model_base_image
        else:
            if not registry_server:
                raise DeployRequestError("Missing MODEL_BASE_IMAGE and REGISTRY_SERVER.", 500)
            base_image = f"{image_repository(registry_server, registry_namespace, 'model-service-base')}:py312"

        if not is_valid_image_ref(base_image):
            raise DeployRequestError(f"Invalid base image '{base_image}'.", 500)

        await self._check_conflicts(
            namespace=namespace,
            deployment_slug=deployment_slug,
            resource_name=resource_name,
            deployed_name=deployed_name,
            overwrite=bool(overwrite),
        )

        operation_id = self._build_operation_id()
        job_name = f"bentloy-build-{deployment_slug}"[:56].rstrip("-") + f"-{operation_id[-6:]}"
        job_name = re.sub(r"[^a-z0-9-]+", "-", job_name.lower()).strip("-")[:63].rstrip("-")

        request: DeployOperationRequest = {
            "namespace": namespace,
            "model_name": model_name,
            "model_slug": model_slug,
            "deployed_name": deployed_name,
            "deployment_slug": deployment_slug,
            "model_version": model_version,
            "run_id": run_id,
            "experiment_name": experiment_name,
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
            "run_id": run_id,
            "model_uri": model_uri,
            "experiment_name": experiment_name,
            "namespace": namespace,
            "replicas": replicas,
            "image_ref": target_image,
            "k8s_job": job_name,
            "error": None,
            "error_log": None,
            "message": None,
            "request": request,
        }

        # Atomic re-check: closes the gap with a concurrent request to this gateway that
        # queued the same resource while we were reading Kubernetes.
        active = await self.operation_store.add_if_no_active(operation)
        if active is not None:
            raise self._in_progress_error(deployed_name, resource_name, active)
        asyncio.create_task(self.deploy_orchestrator.run_operation(operation_id))

        return {
            "operation_id": operation_id,
            "status": "queued",
            "model_name": model_name,
            "deployed_name": deployed_name,
            "run_id": run_id or None,
            "model_uri": model_uri or None,
            "experiment_name": experiment_name or None,
            "namespace": namespace,
            "image_ref": target_image,
            "k8s_job": job_name,
        }

    async def _check_conflicts(
        self,
        *,
        namespace: str,
        deployment_slug: str,
        resource_name: str,
        deployed_name: str,
        overwrite: bool,
    ) -> None:
        """Reject (409) a deploy that would silently replace another one.

        Names that differ but share a slug (`Churn_V2`, `churn v2`) map to the same
        `<prefix>-<slug>` Service/Deployment, and `ensure_resource` patches existing
        resources, so the earlier model would be replaced without notice.

        There is a window between this check and the resources being applied (the build
        takes minutes). Within this gateway it is closed by the operation store: an active
        operation for the same resource blocks new ones, and `add_if_no_active` re-checks
        atomically. What remains (several gateway replicas, or someone applying the
        resources by hand during a build) is rare enough that a distributed lock is not
        worth it.
        """

        active = await self.operation_store.find_active(namespace, deployment_slug)
        if active is not None:
            raise self._in_progress_error(deployed_name, resource_name, active)
        if overwrite:
            return

        try:
            metadata = await self.deploy_orchestrator.find_model_resource(
                namespace, resource_name
            )
        except Exception as exc:
            raise DeployRequestError(
                truncate(
                    f"Could not check whether {resource_name} already exists in namespace "
                    f"'{namespace}': {exc}"
                ),
                503,
            ) from exc
        if metadata is None:
            return

        labels = metadata.get("labels") or {}
        annotations = metadata.get("annotations") or {}
        run_id = labels.get(RUN_ID_LABEL) or ""
        experiment = annotations.get(EXPERIMENT_NAME_ANNOTATION) or labels.get(EXPERIMENT_LABEL) or ""
        raise DeployRequestError(
            f"'{deployed_name}' maps to {resource_name} in namespace '{namespace}', "
            f"already used by {self._describe_owner(run_id, experiment, 'an existing deployment')}. "
            "Use another name or overwrite=true.",
            409,
            extra={
                "conflict": CONFLICT_EXISTS,
                "resource_name": resource_name,
                "run_id": run_id or None,
                "experiment_name": experiment or None,
            },
        )

    @staticmethod
    def _describe_owner(run_id: str, experiment_name: str, fallback: str) -> str:
        owner = f"run {run_id}" if run_id else fallback
        if experiment_name:
            owner += f" (experiment '{experiment_name}')"
        return owner

    def _in_progress_error(
        self, deployed_name: str, resource_name: str, operation: DeployOperationRecord
    ) -> DeployRequestError:
        owner = self._describe_owner(
            operation.get("run_id") or "",
            operation.get("experiment_name") or "",
            f"deployed_name '{operation.get('deployed_name')}'",
        )
        return DeployRequestError(
            f"'{deployed_name}' maps to {resource_name}, which has an operation in progress "
            f"({operation['operation_id']}, status '{operation['status']}') for {owner}. "
            "Wait until it finishes; overwrite=true cannot replace a deployment still in progress.",
            409,
            extra={
                "conflict": CONFLICT_IN_PROGRESS,
                "resource_name": resource_name,
                "operation_id": operation["operation_id"],
                "status": operation["status"],
            },
        )

    async def deploy_status(self, operation_id: str) -> dict[str, Any]:
        """Return the latest status snapshot for a queued deployment operation."""

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
            "run_id": operation.get("run_id") or None,
            "model_uri": operation.get("model_uri") or None,
            "experiment_name": operation.get("experiment_name") or None,
            "namespace": operation["namespace"],
            "image_ref": operation["image_ref"],
            "k8s_job": operation["k8s_job"],
            "error": operation["error"],
            "error_log": operation.get("error_log"),
            "message": operation["message"],
            "created_at": operation["created_at"],
            "updated_at": operation["updated_at"],
        }

    async def delete_operation(self, operation_id: str):

        """Delete the model for a queued deployment operation"""

        operation = await self.operation_store.get(operation_id)

        if not operation:
            return {
                "status": "error",
                "message": f"Operation {operation_id} not found."
            }

        # 2. Update the status so the UI/Client knows it's being deleted
        await self.operation_store.update(operation_id, status="deleting")

        # 3. Spawn the background task for K8s and Registry cleanup

        asyncio.create_task(self.delete_operation_from_store(
            operation_id=operation_id,
            operation_store=self.operation_store,
            registry_secret=self.registry_secret
        ))

        return {"status": "pending deletion"}

    async def delete_operation_from_store(
            self, 
            operation_id: str,
            operation_store: Any,
            registry_secret: str
        ) -> None:
        """Background task to delete K8s resources and container images."""
        
        operation = await operation_store.get(operation_id)
        if not operation:
            return

        # Reuse the settings the operation was built with (falls back to gateway defaults).
        request = operation.get("request") or {}
        registry_secret = request.get("registry_secret") or registry_secret
        insecure_registry_tls = bool(
            request.get("insecure_registry_tls", self.kaniko_skip_tls_verify_default)
        )

        try:
            notes = await self.deploy_orchestrator.delete_operation_resources(
                operation,
                registry_secret=registry_secret,
                insecure_registry_tls=insecure_registry_tls,
            )
            print(f"Deleted operation {operation_id}: {' '.join(notes) or 'nothing to delete.'}")
            await operation_store.delete(operation_id)

        except Exception as e:
            # Log the full error to your console/terminal so you can read it!
            print(f"FAILED to delete operation {operation_id}:")
            traceback.print_exc() 
            
            # Save a better error string
            error_msg = f"{type(e).__name__}: {str(e)}"

            await operation_store.update(
                operation_id,
                status="delete_failed",
                error=truncate(error_msg),
                error_log=error_msg if len(error_msg) > MAX_ERROR_CHARS else None,
            )

    async def delete_model(self, service_name: str):
        """
        Deletes a deployed model completely:
        1. Fetches the Deployment to discover the image_ref and registry_secret.
        2. Deletes the K8s Deployment and Service (which cascades to Pods).
        3. Triggers the background Job to delete the image from the registry.
        """
        return (await self.deploy_orchestrator.delete_deployed_model(service_name=service_name, namespace=self.model_namespace))
        