"""Fault effects through the gateway against real PostgreSQL: state, event and counter
assertions for each implemented effect (IMPLEMENTATION.md M3 acceptance)."""

import asyncio
from typing import Any

import pytest
from sqlalchemy import Engine, text

from agentcrash.domain.models import (
    Delay,
    FaultRule,
    HttpError,
    SchemaMutation,
    TimeoutAfterCommit,
    TimeoutBeforeCommit,
)
from agentcrash.domain.runtime import ToolTimeout
from agentcrash.execution.client import GatewayTransport, HttpToolClient
from agentcrash.execution.gateway import GatewayRefusal
from agentcrash.execution.server import GatewayServer
from agentcrash.storage.store import PostgresStore

from .test_execution import World
from .test_gateway import POST_ARGS, count, open_gateway, req


@pytest.fixture
def world(clean_engine: Engine, store: PostgresStore) -> World:
    return World(clean_engine, store)


KEY = "post:INV-1001"
PO = {"po_id": "PO-2001"}
INV = {"invoice_id": "INV-1001"}


def rule(rid: str, tool: str, effect: Any, **kw: Any) -> FaultRule:
    return FaultRule(id=rid, tool=tool, effect=effect, **kw)


def fault_events(world: World) -> list[dict[str, Any]]:
    with world.engine.connect() as conn:
        rows = conn.execute(
            text("SELECT payload FROM events WHERE type = 'fault.decided' ORDER BY seq")
        )
        return [r.payload for r in rows]


def types(world: World) -> list[str]:
    with world.engine.connect() as conn:
        return [
            r.type
            for r in conn.execute(
                text("SELECT type FROM events WHERE type <> 'attempt.started' ORDER BY seq")
            )
        ]


def test_http_error_returns_the_status_without_touching_state(world: World) -> None:
    r = rule(
        "r429",
        "ledger.post_invoice",
        HttpError(type="http_error", status=429, retry_after_ms=1000),
    )
    gateway, _ = open_gateway(world, faults=(r,))
    first = gateway.tool_call(req("c1", "ledger.post_invoice", POST_ARGS, KEY))
    assert first.status == 429
    assert first.body["error"]["retry_after_ms"] == 1000 and first.body["error"]["injected"]
    assert count(world, "ledger_entries") == 0 and count(world, "command_receipts") == 0
    assert types(world) == ["tool.requested", "policy.decided", "fault.decided", "tool.completed"]
    # The rule is spent: the retry reaches the real tool and commits once.
    second = gateway.tool_call(req("c2", "ledger.post_invoice", POST_ARGS, KEY))
    assert second.status == 200 and count(world, "ledger_entries") == 1
    decided = fault_events(world)
    assert [(d["decision"], d["applied"], d["applications_after"]) for d in decided] == [
        ("applied", True, 1),
        ("exhausted", False, 1),
    ]
    assert [d["tool_ordinal"] for d in decided] == [1, 2]


def test_timeout_before_commit_writes_nothing_and_a_same_key_retry_commits_once(
    world: World,
) -> None:
    r = rule(
        "tb",
        "ledger.post_invoice",
        TimeoutBeforeCommit(type="timeout_before_commit", elapsed_ms=3000),
    )
    gateway, _ = open_gateway(world, faults=(r,))
    with pytest.raises(ToolTimeout) as err:
        gateway.tool_call(req("c1", "ledger.post_invoice", POST_ARGS, KEY))
    assert (err.value.stage, err.value.elapsed_ms) == ("before_commit", 3000)
    assert count(world, "ledger_entries") == 0 and count(world, "command_receipts") == 0
    assert "environment.mutated" not in types(world)
    # A transport retransmit of the same call ID repeats the timeout and allocates no ordinal.
    with pytest.raises(ToolTimeout):
        gateway.tool_call(req("c1", "ledger.post_invoice", POST_ARGS, KEY))
    assert count(world, "tool_calls") == 1
    ok = gateway.tool_call(req("c2", "ledger.post_invoice", POST_ARGS, KEY))
    assert ok.status == 200 and ok.meta.logical_time_ms == 3000  # time advanced by the timeout
    assert count(world, "ledger_entries") == 1


def test_timeout_after_commit_commits_exactly_one_write_and_loses_only_the_response(
    world: World,
) -> None:
    r = rule(
        "ta",
        "ledger.post_invoice",
        TimeoutAfterCommit(type="timeout_after_commit", elapsed_ms=2000),
    )
    gateway, _ = open_gateway(world, faults=(r,))
    with pytest.raises(ToolTimeout) as err:
        gateway.tool_call(req("c1", "ledger.post_invoice", POST_ARGS, KEY))
    assert err.value.stage == "after_commit"
    assert count(world, "ledger_entries") == 1 and count(world, "command_receipts") == 1
    assert types(world) == [
        "tool.requested",
        "policy.decided",
        "fault.decided",
        "environment.mutated",
        "clock.advanced",
        "tool.transport_failed",
    ]  # one mutation, no tool.completed: exactly one terminal event for the call
    # Reconcile through the receipt, then retry the same key: still one write.
    receipt = gateway.tool_call(req("c2", "receipt.get", {"idempotency_key": KEY}))
    assert receipt.status == 200
    again = gateway.tool_call(req("c3", "ledger.post_invoice", POST_ARGS, KEY))
    assert again.status == 200 and again.body == receipt.body["result"]
    assert count(world, "ledger_entries") == 1 and types(world).count("environment.mutated") == 1


def test_delay_advances_logical_time_and_aborts_when_it_cannot_fit(world: World) -> None:
    slow = rule("slow", "purchase_order.get", Delay(type="delay", duration_ms=2000))
    gateway, _ = open_gateway(world, faults=(slow,), logical_ms=5000)
    res = gateway.tool_call(req("c1", "purchase_order.get", PO))
    assert res.status == 200 and res.meta.logical_time_ms == 2000
    assert types(world).count("clock.advanced") == 1

    too_slow = rule("big", "invoice.get", Delay(type="delay", duration_ms=9000))
    gateway2, _ = open_gateway(world, faults=(too_slow,), logical_ms=5000)
    with pytest.raises(GatewayRefusal) as refusal:
        gateway2.tool_call(req("c1", "invoice.get", INV))
    assert refusal.value.code == "BUDGET_EXCEEDED" and gateway2.stop_reason == "budget_exceeded"


def test_ineligible_rules_never_consume_applications(world: World) -> None:
    only_second = rule(
        "second", "invoice.get", HttpError(type="http_error", status=503), on_tool_ordinals=[2]
    )
    other_tool = rule("other", "document.get", HttpError(type="http_error", status=503))
    gateway, _ = open_gateway(world, faults=(only_second, other_tool))
    for i in range(3):  # other tools do not move invoice.get's ordinal
        assert gateway.tool_call(req(f"p{i}", "purchase_order.get", PO)).status == 200
    assert fault_events(world) == []
    assert gateway.tool_call(req("i1", "invoice.get", INV)).status == 200  # ordinal 1: ineligible
    assert fault_events(world) == []
    assert gateway.tool_call(req("i2", "invoice.get", INV)).status == 503  # ordinal 2
    assert gateway.tool_call(req("i3", "invoice.get", INV)).status == 200  # ordinal 3: ineligible
    assert [d["rule_id"] for d in fault_events(world)] == ["second"]
    # The unrelated document.get rule was never eligible, so it still has its full allowance.
    assert gateway.tool_call(req("d1", "document.get", INV)).status == 503


def test_policy_denial_happens_before_faults(world: World) -> None:
    r = rule("r", "purchase_order.get", HttpError(type="http_error", status=429))
    gateway, _ = open_gateway(world, faults=(r,), allowed=frozenset({"invoice.get"}))
    denied = gateway.tool_call(req("c1", "purchase_order.get", PO))
    assert denied.status == 403 and fault_events(world) == []


def test_a_probabilistic_rule_replays_identically_for_one_manifest(world: World) -> None:
    r = rule(
        "coin",
        "invoice.get",
        HttpError(type="http_error", status=503),
        probability="0.5",
        max_applications=1000,
    )
    statuses: list[list[int]] = []
    for _ in range(2):  # two separate attempts, same manifest
        gateway, _ = open_gateway(world, faults=(r,), tool_calls=50)
        statuses.append(
            [gateway.tool_call(req(f"c{i}", "invoice.get", INV)).status for i in range(20)]
        )
    assert statuses[0] == statuses[1] and {200, 503} <= set(statuses[0])


def test_timeout_crosses_the_loopback_transport_as_a_transport_error(world: World) -> None:
    r = rule(
        "tb", "purchase_order.get", TimeoutBeforeCommit(type="timeout_before_commit", elapsed_ms=7)
    )
    gateway, _ = open_gateway(world, faults=(r,))
    server = GatewayServer(gateway, "cap-" + "y" * 40)
    port = server.start()
    try:
        client = HttpToolClient(GatewayTransport(f"http://127.0.0.1:{port}", "cap-" + "y" * 40))
        with pytest.raises(ToolTimeout) as err:
            asyncio.run(client.call(req("c1", "purchase_order.get", PO)))
        assert (err.value.stage, err.value.elapsed_ms) == ("before_commit", 7)
        assert asyncio.run(client.call(req("c2", "purchase_order.get", PO))).status == 200
    finally:
        server.stop()


def completed_events(world: World) -> list[dict[str, Any]]:
    with world.engine.connect() as conn:
        rows = conn.execute(
            text("SELECT payload FROM events WHERE type = 'tool.completed' ORDER BY seq")
        )
        return [r.payload for r in rows]


def test_schema_mutation_changes_only_the_response_body_and_records_both_hashes(
    world: World,
) -> None:
    r = rule(
        "rename",
        "purchase_order.get",
        SchemaMutation(
            type="schema_mutation",
            operation="rename",
            pointer="/approved_amount_minor",
            new_key="approved_amount",
        ),
    )
    gateway, _ = open_gateway(world, faults=(r,))
    mutated = gateway.tool_call(req("c1", "purchase_order.get", PO))
    assert mutated.status == 200 and "approved_amount_minor" not in mutated.body
    assert mutated.body["approved_amount"] == 10000 and mutated.body["version"] == 2
    clean = gateway.tool_call(req("c2", "purchase_order.get", PO))  # rule exhausted
    assert clean.body["approved_amount_minor"] == 10000
    first, second = completed_events(world)
    assert first["mutation"] == {
        "rule_id": "rename",
        "operation": "rename",
        "pointer": "/approved_amount_minor",
        "applied": True,
    }
    assert first["original_body_ref"] == second["body_ref"]  # the original is what a clean read is
    assert first["body_ref"] != first["original_body_ref"] and "mutation" not in second


def test_schema_mutation_on_an_error_response_is_recorded_as_not_applied(world: World) -> None:
    r = rule(
        "rm",
        "purchase_order.get",
        SchemaMutation(type="schema_mutation", operation="remove", pointer="/currency"),
    )
    gateway, _ = open_gateway(world, faults=(r,))
    missing = gateway.tool_call(req("c1", "purchase_order.get", {"po_id": "PO-9999"}))
    assert missing.status == 404
    (event,) = completed_events(world)
    assert event["mutation"]["applied"] is False
    assert event["mutation"]["reason"] == "non_success_status"
    assert "original_body_ref" not in event
