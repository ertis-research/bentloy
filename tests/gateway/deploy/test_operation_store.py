"""Tests for the in-memory DeployOperationStore."""

from gateway.app.deploy.operation_store import DeployOperationStore
from tests.gateway.fakes import RUN_ID, build_operation, build_request


async def test_update_and_summary_sorting() -> None:
    """Updates refresh ``updated_at`` and summaries are sorted by most recent update first."""
    timestamps = iter(["2026-04-15T10:00:01+00:00", "2026-04-15T10:00:02+00:00"])
    store = DeployOperationStore(now_iso=lambda: next(timestamps))
    op_a = build_operation(
        operation_id="op_a",
        request=build_request("build-model-a"),
        updated_at="2026-04-15T10:00:00+00:00",
    )
    op_b = build_operation(
        operation_id="op_b",
        request=build_request("build-model-b"),
        updated_at="2026-04-15T09:59:59+00:00",
    )

    await store.add(op_a)
    await store.add(op_b)
    await store.update("op_b", status="building", step="create_job")

    updated = await store.get("op_b")
    assert updated is not None
    assert updated["status"] == "building"
    assert updated["updated_at"] == "2026-04-15T10:00:01+00:00"

    summaries = await store.list_summaries(limit=2)
    assert [item["operation_id"] for item in summaries] == ["op_b", "op_a"]
    assert summaries[0]["run_id"] == RUN_ID
    assert summaries[0]["model_uri"] == f"runs:/{RUN_ID}/ludwig-model"
    assert summaries[0]["experiment_name"] == "Tiny Demo"


async def test_list_summaries_filters_by_experiment_and_run() -> None:
    """Summaries can be filtered by exact experiment name and run id; legacy records report null."""
    store = DeployOperationStore(now_iso=lambda: "2026-04-15T10:00:00+00:00")
    other_run = "f" * 32
    other_request = {
        **build_request("build-model-b"),
        "run_id": other_run,
        "experiment_name": "Other",
    }
    await store.add(build_operation("op_a", build_request("build-model-a")))
    await store.add(build_operation("op_b", other_request))  # type: ignore[arg-type]
    legacy = build_operation("op_c", build_request("build-model-c"))
    for key in ("run_id", "model_uri", "experiment_name"):
        legacy.pop(key)  # type: ignore[misc]
    await store.add(legacy)

    by_experiment = await store.list_summaries(experiment_name="Tiny Demo")
    assert [item["operation_id"] for item in by_experiment] == ["op_a"]
    # Exact match: no substring or case-insensitive matching.
    assert await store.list_summaries(experiment_name="tiny demo") == []
    assert await store.list_summaries(experiment_name="Tiny") == []
    by_run = await store.list_summaries(run_id=other_run)
    assert [item["operation_id"] for item in by_run] == ["op_b"]

    everything = {item["operation_id"]: item for item in await store.list_summaries()}
    assert set(everything) == {"op_a", "op_b", "op_c"}
    # Operations recorded without these fields report null.
    assert everything["op_c"]["run_id"] is None
    assert everything["op_c"]["model_uri"] is None
    assert everything["op_c"]["experiment_name"] is None
