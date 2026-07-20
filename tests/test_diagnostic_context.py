from __future__ import annotations

import anyio
import pytest

from wybra.diagnostics import (
    DiagnosticsCapability,
    current_diagnostic_context,
    diagnostic_context,
    record_sql_query,
)
from wybra.events import EVT_SQL


@pytest.mark.anyio
async def test_explicit_diagnostic_context_retains_a_correlated_snapshot() -> None:
    capability = DiagnosticsCapability(allowed_scopes=(EVT_SQL,))

    async with diagnostic_context(
        capability,
        kind="task",
        description="rebuild article index",
        level="trace",
    ) as context:
        assert current_diagnostic_context() == context
        record_sql_query("select 1", duration_seconds=0.01)

    snapshot = capability.snapshots(EVT_SQL)[0]

    assert snapshot.summary["context"] == {
        "id": context.identifier,
        "parent_id": None,
        "kind": "task",
        "description": "rebuild article index",
    }
    assert current_diagnostic_context() is None


@pytest.mark.anyio
async def test_concurrent_diagnostic_contexts_do_not_mix_events() -> None:
    capability = DiagnosticsCapability(allowed_scopes=(EVT_SQL,))

    async def collect(statement: str) -> None:
        async with diagnostic_context(
            capability,
            kind="task",
            description=statement,
            level="trace",
        ):
            record_sql_query(statement, duration_seconds=0.01)

    async with anyio.create_task_group() as task_group:
        task_group.start_soon(collect, "select 1")
        task_group.start_soon(collect, "select 2")

    statements = {
        snapshot.events[0]["attributes"]["statement"]
        for snapshot in capability.snapshots(EVT_SQL)
    }
    assert statements == {"select 1", "select 2"}


def test_context_free_observation_is_retained_by_the_process_collector() -> None:
    capability = DiagnosticsCapability(allowed_scopes=(EVT_SQL,), level="trace")
    from wybra.diagnostics.capabilities import activate_process_diagnostics

    activate_process_diagnostics(capability)
    try:
        record_sql_query("select startup state", duration_seconds=0.01)
    finally:
        from wybra.diagnostics.capabilities import deactivate_process_diagnostics

        deactivate_process_diagnostics(capability)

    assert capability.snapshots(EVT_SQL)[0].events[0]["name"] == "statement"
