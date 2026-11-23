"""Asynchronous in-memory storage for deployment operation state."""

import asyncio
from copy import deepcopy
from datetime import datetime, timezone
from typing import Any, Callable

from ..domain.types import DeployOperationRecord

# Statuses of an operation that will still build/apply its image.
DEPLOYING_STATUSES = frozenset({"queued", "building", "pushing", "deploying"})
# Statuses of an operation still acting on its model-service resources.
ACTIVE_STATUSES = DEPLOYING_STATUSES | {"deleting"}


def _targets(operation: DeployOperationRecord, namespace: str, deployment_slug: str) -> bool:
    """Return whether an operation deploys to `<prefix>-<deployment_slug>` in `namespace`."""

    request = operation.get("request") or {}
    return (
        request.get("namespace") == namespace
        and request.get("deployment_slug") == deployment_slug
    )


def _now_iso_utc() -> str:
    """Return the current UTC timestamp in ISO-8601 format."""

    return datetime.now(timezone.utc).isoformat()


class DeployOperationStore:
    """In-memory async-safe store for deployment operations."""

    def __init__(self, now_iso: Callable[[], str] | None = None):
        self._operations: dict[str, DeployOperationRecord] = {}
        self._lock = asyncio.Lock()
        self._now_iso = now_iso or _now_iso_utc

    async def add(self, operation: DeployOperationRecord) -> None:
        """Store a new operation record."""

        operation_id = operation["operation_id"]
        async with self._lock:
            self._operations[operation_id] = deepcopy(operation)

    async def add_if_no_active(
        self, operation: DeployOperationRecord
    ) -> DeployOperationRecord | None:
        """Store `operation` unless an active one targets the same resource.

        Check and insert happen under one lock, so two concurrent requests in this
        process cannot both queue a deploy of the same resource. Returns a copy of the
        conflicting operation (and stores nothing), or None once stored.
        """

        request = operation["request"]
        async with self._lock:
            conflict = self._find_active(request["namespace"], request["deployment_slug"])
            if conflict is not None:
                return deepcopy(conflict)
            self._operations[operation["operation_id"]] = deepcopy(operation)
            return None

    async def find_active(
        self, namespace: str, deployment_slug: str
    ) -> DeployOperationRecord | None:
        """Return a copy of an active operation targeting the resource, if any."""

        async with self._lock:
            operation = self._find_active(namespace, deployment_slug)
            return deepcopy(operation) if operation is not None else None

    def _find_active(
        self, namespace: str, deployment_slug: str
    ) -> DeployOperationRecord | None:
        for operation in self._operations.values():
            if operation.get("status") in ACTIVE_STATUSES and _targets(
                operation, namespace, deployment_slug
            ):
                return operation
        return None

    async def find_deploying_image(
        self, image_ref: str, exclude_operation_id: str = ""
    ) -> DeployOperationRecord | None:
        """Return a copy of another operation that is still deploying `image_ref`, if any."""

        async with self._lock:
            for operation in self._operations.values():
                if (
                    operation["operation_id"] != exclude_operation_id
                    and operation.get("status") in DEPLOYING_STATUSES
                    and operation.get("image_ref") == image_ref
                ):
                    return deepcopy(operation)
        return None

    async def get(self, operation_id: str) -> DeployOperationRecord | None:
        """Fetch a copy of an operation by id."""

        async with self._lock:
            operation = self._operations.get(operation_id)
            if operation is None:
                return None
            return deepcopy(operation)

    async def update(self, operation_id: str, **changes: Any) -> None:
        """Apply partial changes to an existing operation and refresh its timestamp."""

        async with self._lock:
            operation = self._operations.get(operation_id)
            if operation is None:
                return
            operation.update(changes)
            operation["updated_at"] = self._now_iso()

    async def list_summaries(
        self,
        limit: int = 20,
        experiment_name: str = "",
        run_id: str = "",
    ) -> list[dict[str, Any]]:
        """Return recent operation summaries sorted by most recent update.

        Non-empty `experiment_name` / `run_id` keep only exact matches.
        """

        async with self._lock:
            summaries = [
                {
                    "operation_id": operation["operation_id"],
                    "status": operation["status"],
                    "model_name": operation["model_name"],
                    "deployed_name": operation["deployed_name"],
                    "image_ref": operation["image_ref"],
                    "updated_at": operation["updated_at"],
                    "error": operation.get("error"),
                    "run_id": operation.get("run_id") or None,
                    "model_uri": operation.get("model_uri") or None,
                    "experiment_name": operation.get("experiment_name") or None,
                }
                for operation in self._operations.values()
                if (not experiment_name or operation.get("experiment_name") == experiment_name)
                and (not run_id or operation.get("run_id") == run_id)
            ]

        summaries.sort(key=lambda item: item["updated_at"], reverse=True)
        return summaries[:limit]

    async def delete(self, operation_id: str) -> None:
        """Completely remove an operation from the store to free up memory."""
        async with self._lock:
            # pop() removes the key if it exists, and does nothing if it doesn't
            self._operations.pop(operation_id, None)