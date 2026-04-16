import asyncio
from copy import deepcopy
from datetime import datetime, timezone
from typing import Any, Callable

from ..domain.types import DeployOperationRecord


def _now_iso_utc() -> str:
    return datetime.now(timezone.utc).isoformat()


class DeployOperationStore:
    """In-memory async-safe store for deployment operations."""

    def __init__(self, now_iso: Callable[[], str] | None = None):
        self._operations: dict[str, DeployOperationRecord] = {}
        self._lock = asyncio.Lock()
        self._now_iso = now_iso or _now_iso_utc

    async def add(self, operation: DeployOperationRecord) -> None:
        operation_id = operation["operation_id"]
        async with self._lock:
            self._operations[operation_id] = deepcopy(operation)

    async def get(self, operation_id: str) -> DeployOperationRecord | None:
        async with self._lock:
            operation = self._operations.get(operation_id)
            if operation is None:
                return None
            return deepcopy(operation)

    async def update(self, operation_id: str, **changes: Any) -> None:
        async with self._lock:
            operation = self._operations.get(operation_id)
            if operation is None:
                return
            operation.update(changes)
            operation["updated_at"] = self._now_iso()

    async def list_summaries(self, limit: int = 20) -> list[dict[str, str]]:
        async with self._lock:
            summaries = [
                {
                    "operation_id": operation["operation_id"],
                    "status": operation["status"],
                    "model_name": operation["model_name"],
                    "deployed_name": operation["deployed_name"],
                    "image_ref": operation["image_ref"],
                    "updated_at": operation["updated_at"],
                }
                for operation in self._operations.values()
            ]

        summaries.sort(key=lambda item: item["updated_at"], reverse=True)
        return summaries[:limit]
