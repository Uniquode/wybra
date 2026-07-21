from __future__ import annotations

import pytest

from wybra.diagnostics.capabilities import DiagnosticsCapability
from wybra.diagnostics.records import RequestDiagnostics
from wybra.events._core import EVT_SQL, EVT_TEMPLATE, SQL_STATEMENT


def _completed_diagnostics(*, statement: str) -> RequestDiagnostics:
    diagnostics = RequestDiagnostics(method="GET", path="/articles", level="trace")
    diagnostics.record_sql_query(statement, duration_seconds=0.01)
    diagnostics.record_template_render("articles.html", duration_seconds=0.01)
    diagnostics.finish(
        route_name="articles",
        status_code=200,
        exception_type=None,
        duration_seconds=0.02,
    )
    return diagnostics


@pytest.mark.anyio
async def test_capability_filters_snapshots_and_notifies_matching_subscription() -> (
    None
):
    capability = DiagnosticsCapability(retention_limit=2)
    subscription = await capability.subscribe((EVT_SQL,))
    capability.record_completed(_completed_diagnostics(statement="select 1"))

    snapshot = await subscription.receive()

    assert [event["category"] for event in snapshot.events] == ["sql"]
    assert [event["attributes"]["statement"] for event in snapshot.events] == [
        "select 1"
    ]
    assert len(capability.snapshots(EVT_TEMPLATE)) == 1


@pytest.mark.anyio
async def test_capability_rejects_subscription_that_expands_collector_filter() -> None:
    capability = DiagnosticsCapability(allowed_scopes=(EVT_SQL,))

    with pytest.raises(ValueError, match="expands the collector filter"):
        await capability.subscribe((EVT_TEMPLATE,))


def test_capability_retains_a_bounded_history() -> None:
    capability = DiagnosticsCapability(retention_limit=1)
    capability.record_completed(_completed_diagnostics(statement="select 1"))
    capability.record_completed(_completed_diagnostics(statement="select 2"))

    snapshots = capability.snapshots(EVT_SQL)

    assert len(snapshots) == 1
    assert snapshots[0].events[0]["attributes"]["statement"] == "select 2"


def test_sql_statement_topic_selects_emitted_statement_events() -> None:
    capability = DiagnosticsCapability()
    capability.record_completed(_completed_diagnostics(statement="select 1"))

    snapshots = capability.snapshots(EVT_SQL(SQL_STATEMENT))

    assert snapshots[0].events[0]["name"] == "statement"


def test_capability_replaces_its_collector_scope_for_later_snapshots() -> None:
    capability = DiagnosticsCapability(allowed_scopes=(EVT_SQL,))
    capability.record_completed(_completed_diagnostics(statement="select 1"))
    capability.replace_collector_scopes((EVT_TEMPLATE,))
    capability.record_completed(_completed_diagnostics(statement="select 2"))

    assert len(capability.snapshots(EVT_SQL)) == 1
    assert len(capability.snapshots(EVT_TEMPLATE)) == 1


def test_capability_marks_a_snapshot_when_its_events_are_truncated() -> None:
    capability = DiagnosticsCapability(snapshot_event_limit=1)
    capability.record_completed(_completed_diagnostics(statement="select 1"))

    snapshot = capability.snapshots(EVT_SQL, include_empty=True)[0]

    assert snapshot.truncated is True
    assert len(snapshot.events) == 1


def test_capability_retains_a_completed_context_without_selected_events() -> None:
    capability = DiagnosticsCapability(allowed_scopes=(EVT_SQL,))
    diagnostics = RequestDiagnostics(method="GET", path="/", level="info")
    diagnostics.finish(
        route_name="status",
        status_code=200,
        exception_type=None,
        duration_seconds=0.01,
    )

    capability.record_completed(diagnostics)

    snapshot = capability.snapshots(EVT_SQL, include_empty=True)[0]
    assert snapshot.summary["route"] == "status"
    assert snapshot.events == ()


@pytest.mark.anyio
async def test_subscription_reports_dropped_notifications() -> None:
    capability = DiagnosticsCapability(subscription_queue_limit=1)
    subscription = await capability.subscribe((EVT_SQL,))
    capability.record_completed(_completed_diagnostics(statement="select 1"))
    capability.record_completed(_completed_diagnostics(statement="select 2"))

    await subscription.receive()

    assert subscription.take_dropped() is True
    assert subscription.take_dropped() is False
