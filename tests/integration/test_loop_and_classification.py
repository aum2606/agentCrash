"""Loop warning, hard-budget termination and stored failure classification, against real
PostgreSQL: the gateway as a driver, then whole runs through the worker and child process."""

from typing import Any
from uuid import UUID

import pytest
from sqlalchemy import Engine, text

from agentcrash.domain.models import FaultRule, HttpError
from agentcrash.execution.gateway import GatewayRefusal
from agentcrash.storage.store import PostgresStore

from .helpers import submit_run
from .test_api_flow import AUTH
from .test_execution import World, stored
from .test_gateway import POST_ARGS, open_gateway, req

INV = {"invoice_id": "INV-1001"}
KEY = "post:INV-1001"


@pytest.fixture
def world(clean_engine: Engine, store: PostgresStore) -> World:
    return World(clean_engine, store)


def completed(world: World) -> list[dict[str, Any]]:
    with world.engine.connect() as conn:
        rows = conn.execute(
            text(
                "SELECT payload FROM events WHERE type IN ('tool.completed',"
                " 'tool.transport_failed') ORDER BY seq"
            )
        )
        return [r.payload for r in rows]


def flagged(world: World) -> list[bool]:
    return ["loop_suspected" in p for p in completed(world)]


# --- the heuristic, driven directly through the gateway ---------------------------------------


def test_three_identical_reads_in_a_row_raise_the_loop_warning_on_the_third(world: World) -> None:
    gateway, _ = open_gateway(world)
    for i in range(4):
        gateway.tool_call(req(f"c{i}", "invoice.get", INV))
    assert flagged(world) == [False, False, True, True]  # still looping on the fourth


def test_a_different_call_in_between_resets_the_streak(world: World) -> None:
    gateway, _ = open_gateway(world)
    gateway.tool_call(req("c1", "invoice.get", INV))
    gateway.tool_call(req("c2", "invoice.get", INV))
    gateway.tool_call(req("c3", "purchase_order.get", {"po_id": "PO-2001"}))
    gateway.tool_call(req("c4", "invoice.get", INV))
    assert not any(flagged(world))


def test_a_changed_outcome_is_progress_so_retry_then_success_is_not_a_loop(world: World) -> None:
    flaky = FaultRule(
        id="flaky",
        tool="invoice.get",
        effect=HttpError(type="http_error", status=503),
        max_applications=2,
    )
    gateway, _ = open_gateway(world, faults=(flaky,))
    statuses = [gateway.tool_call(req(f"c{i}", "invoice.get", INV)).status for i in range(3)]
    assert statuses == [503, 503, 200] and not any(flagged(world))


def test_repeated_writes_are_never_flagged_as_a_loop(world: World) -> None:
    gateway, _ = open_gateway(world)
    for i in range(4):
        gateway.tool_call(req(f"c{i}", "ledger.post_invoice", POST_ARGS, KEY))
    assert not any(flagged(world))


def test_a_hard_call_budget_terminates_the_looping_agent_with_complete_evidence(
    world: World,
) -> None:
    gateway, claim = open_gateway(world, tool_calls=5)
    for i in range(5):
        gateway.tool_call(req(f"c{i}", "invoice.get", INV))
    with pytest.raises(GatewayRefusal) as refusal:
        gateway.tool_call(req("c5", "invoice.get", INV))
    assert refusal.value.code == "BUDGET_EXCEEDED" and gateway.stop_reason == "budget_exceeded"
    assert len(completed(world)) == 5  # the refused call left no half-recorded trace
    assert world.one("SELECT count(*) FROM events WHERE type = 'tool.requested'") == 5


# --- whole runs: classification is stored and served ------------------------------------------


def run_case(world: World, scenario: str, agent: str, **budgets: int) -> tuple[UUID, Any, Any]:
    run_id = submit_run(world.store, scenario=scenario, agent=agent, budgets=budgets or None)
    world.drain(world.worker())
    (attempt,) = world.attempts(run_id)
    return run_id, attempt, stored(world, attempt.id)


def evaluation(world: World) -> Any:
    with world.engine.connect() as conn:
        return conn.execute(text("SELECT * FROM evaluations")).one()


def test_a_passing_run_stores_no_primary_reason(world: World) -> None:
    run_case(world, "invoice-429", "invoice-resilient")
    row = evaluation(world)
    assert (row.verdict, row.primary_reason, row.findings) == ("pass", None, [])


def test_permanent_failure_is_bounded_reported_and_classified(world: World) -> None:
    run_id, attempt, ev = run_case(world, "neg-permanent-failure", "invoice-resilient")
    calls = [e.payload["tool"] for e in ev.events if e.type == "tool.requested"]
    assert calls.count("invoice.get") == 3  # at most three attempts, then stop
    assert world.one("SELECT count(*) FROM ledger_entries") == 0  # nothing guessed or posted
    assert attempt.termination == "agent_completed"  # it reported inability; it did not crash
    row = evaluation(world)
    assert row.verdict == "fail" and row.primary_reason == "RETRY_EXHAUSTED"
    codes = {f["code"] for f in row.findings}
    assert codes == {"RETRY_EXHAUSTED", "TASK_INCOMPLETE", "LOOP_SUSPECTED"}  # all kept
    served = world.api().get(f"/api/v1/runs/{run_id}/evaluations", headers=AUTH).json()["items"]
    assert served[0]["primary_reason"] == "RETRY_EXHAUSTED"
    assert {f["code"] for f in served[0]["findings"]} == codes


def test_an_unsafe_candidate_is_classified_by_safety_not_task(world: World) -> None:
    run_case(world, "invoice-document-injection", "invoice-naive")
    row = evaluation(world)
    assert row.verdict == "fail" and row.primary_reason == "UNAUTHORIZED_ATTEMPT"
    assert [f["code"] for f in row.findings] == ["UNAUTHORIZED_ATTEMPT"]  # the post was right


def test_an_unhandled_schema_change_names_its_tool(world: World) -> None:
    run_case(world, "invoice-schema-change", "invoice-naive")
    row = evaluation(world)
    assert row.primary_reason == "SCHEMA_UNHANDLED"
    detail = {f["code"]: f["detail"] for f in row.findings}
    assert detail["SCHEMA_UNHANDLED"] == "purchase_order.get" and "TASK_INCOMPLETE" in detail


def test_a_resource_limit_terminates_a_real_run_and_is_reported_after_the_task_failure(
    world: World,
) -> None:
    _, attempt, ev = run_case(world, "invoice-healthy", "invoice-resilient", tool_calls=2)
    assert attempt.termination == "budget_exceeded"
    assert [e.payload["tool"] for e in ev.events if e.type == "tool.requested"] == [
        "invoice.get",
        "document.get",
    ]
    row = evaluation(world)
    assert row.verdict == "fail" and row.primary_reason == "TASK_INCOMPLETE"
    assert {f["code"] for f in row.findings} == {"TASK_INCOMPLETE", "BUDGET_EXCEEDED"}
