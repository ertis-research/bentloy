"""Asynchronous in-memory storage for deployment operation state."""

import asyncio
from copy import deepcopy
from datetime import datetime, timezone
from typing import Any, Callable

from ..domain.types import DeployOperationRecord


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

    async def list_summaries(self, limit: int = 20) -> list[dict[str, str]]:
        """Return recent operation summaries sorted by most recent update."""

        async with self._lock:
            summaries = [
                {
                    "operation_id": operation["operation_id"],
                    "status": operation["status"],
                    "model_name": operation["model_name"],
                    "deployed_name": operation["deployed_name"],
                    "image_ref": operation["image_ref"],
                    "updated_at": operation["updated_at"],
                    "error": operation.get("error")
                }
                for operation in self._operations.values()
            ]

        summaries.sort(key=lambda item: item["updated_at"], reverse=True)
        return summaries[:limit]

    async def delete(self, operation_id: str) -> None:
        """Completely remove an operation from the store to free up memory."""
        async with self._lock:
            # pop() removes the key if it exists, and does nothing if it doesn't
            self._operations.pop(operation_id, None)