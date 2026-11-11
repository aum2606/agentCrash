"""Gateway semantics against real PostgreSQL, without a child process: idempotency, CAS, policy,
redaction, budgets, the transport capability and per-attempt call serialization."""

import threading
from datetime import UTC, datetime
from typing import Any

import pytest
from sqlalchemy import text

from agentcrash.domain.events import PendingEvent
from agentcrash.domain.models import FaultRule
from agentcrash.domain.runtime import ToolRequest
from agentcrash.environments.invoice import catalog
from agentcrash.environments.invoice.fixture import load_bundle
from agentcrash.execution.client import GatewayError, GatewayTransport
from agentcrash.execution.gateway import AttemptPolicy, Gateway, GatewayRefusal, Hooks
from agentcrash.execution.server import GatewayServer

from .helpers import ROOT, submit_run
from .test_execution import World

POST_ARGS: dict[str, Any] = {
    "invoice_id": "INV-1001",
    "po_id": "PO-2001",
    "amount_minor": 10000,
    "currency": "USD",
    "expected_invoice_version": 1,
    "source_po_version": 2,
}


@pytest.fixture
def world(clean_engine: Any, store: Any) -> World:
    return World(clean_engine, store)


def open_gateway(
    world: World,
    hooks: Hooks | None = None,
    faults: tuple[FaultRule, ...] = (),
    allowed: frozenset[str] | None = None,
    **budget: int,
) -> tuple[Gateway, Any]:
    run_id = submit_run(world.store, budgets=budget or None)
    claim = world.queue.claim_execute("w1")
    assert claim is not None and claim.run_id == run_id
    manifest = world.repo.manifest(run_id)
    bundle = load_bundle(ROOT / "fixtures" / "invoice" / "v1" / "standard.json")
    world.repo.start_attempt(
        claim.attempt_id,
        claim.fencing_token,
        bundle.rows(),
        PendingEvent("attempt.started", 0, None, None, {}, datetime.now(UTC)),
    )
    gateway = Gateway(
        world.repo.gate(claim.attempt_id, claim.fencing_token),
        AttemptPolicy(
            "agent",
            allowed or frozenset(manifest.scenario.policy.allowed_tools),
            frozenset(manifest.scenario.policy.forbidden_tools),
            dict(catalog.TOOLS),
            manifest.budgets,
            faults=faults,
            seed=manifest.seed,
            scenario_hash=manifest.scenario_hash,
        ),
        lambda: datetime.now(UTC),
        hooks or Hooks(),
    )
    return gateway, claim


def req(call_id: str, tool: str, args: dict[str, Any], key: str | None = None) -> ToolRequest:
    return ToolRequest(call_id=call_id, actor_id="agent", tool=tool, args=args, idempotency_key=key)


def count(world: World, table: str) -> int:
    return int(world.one(f"SELECT count(*) FROM {table}"))  # noqa: S608 - fixed test table names


def test_a_retransmitted_call_id_returns_the_stored_outcome_and_no_new_ordinal(
    world: World,
) -> None:
    gateway, _ = open_gateway(world)
    first = gateway.tool_call(req("c1", "invoice.get", {"invoice_id": "INV-1001"}))
    again = gateway.tool_call(req("c1", "invoice.get", {"invoice_id": "INV-1001"}))
    assert again == first and first.meta.call_index == 1 and first.meta.tool_ordinal == 1
    assert count(world, "tool_calls") == 1 and count(world, "events") == 1 + 3  # start + 3
    # A logical retry uses a new call id and gets the next ordinal.
    nxt = gateway.tool_call(req("c2", "invoice.get", {"invoice_id": "INV-1001"}))
    assert (nxt.meta.call_index, nxt.meta.tool_ordinal) == (2, 2)


def test_the_same_call_id_with_different_arguments_is_rejected(world: World) -> None:
    gateway, _ = open_gateway(world)
    gateway.tool_call(req("c1", "invoice.get", {"invoice_id": "INV-1001"}))
    with pytest.raises(GatewayRefusal) as err:
        gateway.tool_call(req("c1", "invoice.get", {"invoice_id": "INV-9999"}))
    assert err.value.code == "CALL_ID_CONFLICT" and count(world, "tool_calls") == 1


def test_the_same_write_key_returns_one_receipt_across_new_call_ids(world: World) -> None:
    gateway, _ = open_gateway(world)
    first = gateway.tool_call(req("c1", "ledger.post_invoice", POST_ARGS, "post:INV-1001"))
    second = gateway.tool_call(req("c2", "ledger.post_invoice", POST_ARGS, "post:INV-1001"))
    assert first.status == second.status == 200 and first.body == second.body
    assert count(world, "ledger_entries") == 1 and count(world, "command_receipts") == 1
    types = world.one("SELECT string_agg(type, ',' ORDER BY seq) FROM events")
    assert types.count("environment.mutated") == 1  # the replay mutated nothing
    receipt = gateway.tool_call(req("c3", "receipt.get", {"idempotency_key": "post:INV-1001"}))
    assert receipt.status == 200 and receipt.body["result"] == first.body


def test_a_changed_payload_under_the_same_key_conflicts_and_changes_nothing(world: World) -> None:
    gateway, _ = open_gateway(world)
    gateway.tool_call(req("c1", "ledger.post_invoice", POST_ARGS, "post:INV-1001"))
    changed = gateway.tool_call(
        req("c2", "ledger.post_invoice", {**POST_ARGS, "amount_minor": 9999}, "post:INV-1001")
    )
    assert changed.status == 409 and changed.body["error"]["code"] == "IDEMPOTENCY_CONFLICT"
    assert count(world, "ledger_entries") == 1


def test_a_different_key_cannot_post_the_invoice_twice(world: World) -> None:
    gateway, _ = open_gateway(world)
    ok = gateway.tool_call(req("c1", "ledger.post_invoice", POST_ARGS, "key-a"))
    dup = gateway.tool_call(
        req("c2", "ledger.post_invoice", {**POST_ARGS, "expected_invoice_version": 2}, "key-b")
    )
    assert dup.status == 409 and dup.body["error"]["code"] == "ALREADY_POSTED"
    assert dup.body["error"]["original_receipt_id"] == ok.body["receipt_id"]
    assert count(world, "ledger_entries") == 1 and count(world, "command_receipts") == 1


def test_a_cas_conflict_leaves_no_receipt_so_the_same_key_can_retry(world: World) -> None:
    gateway, _ = open_gateway(world)
    stale = gateway.tool_call(
        req("c1", "ledger.post_invoice", {**POST_ARGS, "expected_invoice_version": 7}, "post:1")
    )
    assert stale.status == 409 and stale.body["error"] == {
        "code": "VERSION_CONFLICT",
        "expected": 7,
        "current": 1,
    }
    assert count(world, "command_receipts") == 0 and count(world, "ledger_entries") == 0
    retry = gateway.tool_call(req("c2", "ledger.post_invoice", POST_ARGS, "post:1"))
    assert retry.status == 200 and count(world, "ledger_entries") == 1


@pytest.mark.parametrize(
    ("args", "key", "field"),
    [
        ({**POST_ARGS, "amount_minor": 1.5}, "k", None),  # floats never enter hashed data
        ({**POST_ARGS, "amount_minor": True}, "k", "amount_minor"),
        ({**POST_ARGS, "currency": "usd"}, "k", "currency"),
        ({**POST_ARGS, "amount_minor": -1}, "k", "amount_minor"),
        ({k: v for k, v in POST_ARGS.items() if k != "po_id"}, "k", "arguments"),
        (POST_ARGS, None, None),  # the semantic key is required
    ],
)
def test_malformed_writes_are_422_and_never_mutate(
    world: World, args: dict[str, Any], key: str | None, field: str | None
) -> None:
    gateway, _ = open_gateway(world)
    if isinstance(args.get("amount_minor"), float):
        with pytest.raises(GatewayRefusal) as err:
            gateway.tool_call(req("c1", "ledger.post_invoice", args, key))
        assert err.value.status == 422
    else:
        resp = gateway.tool_call(req("c1", "ledger.post_invoice", args, key))
        assert resp.status == 422
        assert resp.body["error"].get("field") == field
    assert count(world, "ledger_entries") == 0 and count(world, "command_receipts") == 0


def test_the_vendor_tool_is_denied_recorded_as_an_unsafe_attempt_and_never_runs(
    world: World,
) -> None:
    gateway, _ = open_gateway(world)
    resp = gateway.tool_call(
        req(
            "c1",
            "vendor.update_bank_account",
            {"vendor_id": "V-3001", "bank_account_ref": "attacker", "expected_version": 1},
            "k",
        )
    )
    assert resp.status == 403 and resp.body["error"]["code"] == "TOOL_FORBIDDEN"
    decided = world.one("SELECT payload FROM events WHERE type = 'policy.decided'")
    assert decided["decision"] == "deny" and decided["unsafe_attempt"] is True
    assert (
        world.one(
            "SELECT data->>'bank_account_ref' FROM environment_rows WHERE entity_type = 'vendor'"
        )
        == "synthetic-bank-1"
    )
    assert count(world, "environment_rows") == 4  # untouched fixture rows only


def test_unknown_tools_are_404_and_still_get_exactly_one_policy_decision(world: World) -> None:
    gateway, _ = open_gateway(world)
    resp = gateway.tool_call(req("c1", "shell.exec", {"cmd": "echo hi"}))
    assert resp.status == 404 and resp.body["error"]["code"] == "UNKNOWN_TOOL"
    assert world.one("SELECT count(*) FROM events WHERE type = 'policy.decided'") == 1


def test_credentials_and_canaries_are_redacted_before_persistence(world: World) -> None:
    gateway, _ = open_gateway(world)
    canary = "sk-CANARY1234567890abcdefgh"
    gateway.tool_call(req("c1", "invoice.get", {"invoice_id": "INV-1001", "api_key": canary}))
    gateway.tool_call(req("c2", "invoice.get", {"invoice_id": f"Bearer {canary}"}))
    gateway.agent_step(f"using {canary} and password=hunter2")
    dumped = " ".join(
        str(world.one(sql))
        for sql in (
            "SELECT string_agg(payload::text, ' ') FROM events",
            "SELECT string_agg(outcome::text, ' ') FROM tool_calls",
        )
    )
    assert "CANARY" not in dumped and "[REDACTED]" in dumped


def test_oversized_arguments_are_refused_before_anything_is_written(world: World) -> None:
    gateway, _ = open_gateway(world)
    before = count(world, "events")
    with pytest.raises(GatewayRefusal) as err:
        gateway.tool_call(req("c1", "invoice.get", {"invoice_id": "x" * 70_000}))
    assert err.value.code == "ARGUMENTS_TOO_LARGE" and count(world, "events") == before


def test_a_foreign_actor_is_refused(world: World) -> None:
    gateway, _ = open_gateway(world)
    with pytest.raises(GatewayRefusal) as err:
        gateway.tool_call(
            ToolRequest(
                call_id="c1",
                actor_id="someone-else",
                tool="invoice.get",
                args={"invoice_id": "INV-1001"},
            )
        )
    assert (err.value.code, err.value.status) == ("ACTOR_MISMATCH", 403)


def test_the_step_budget_stops_the_attempt(world: World) -> None:
    gateway, _ = open_gateway(world, steps=2)
    gateway.agent_step("one")
    gateway.agent_step("two")
    with pytest.raises(GatewayRefusal) as err:
        gateway.agent_step("three")
    assert err.value.code == "BUDGET_EXCEEDED" and gateway.stop_reason == "budget_exceeded"


def test_the_logical_clock_advances_in_events_and_respects_its_budget(world: World) -> None:
    gateway, _ = open_gateway(world, logical_ms=1000)
    assert gateway.advance_clock(400) == 400 and gateway.advance_clock(500) == 900
    seen = gateway.tool_call(req("c1", "invoice.get", {"invoice_id": "INV-1001"}))
    assert seen.meta.logical_time_ms == 900
    with pytest.raises(GatewayRefusal):
        gateway.advance_clock(101)
    assert gateway.stop_reason == "budget_exceeded"
    assert world.one("SELECT count(*) FROM events WHERE type = 'clock.advanced'") == 2


# --- the loopback transport ---------------------------------------------------------------


def test_the_transport_demands_the_capability_and_serializes_calls(world: World) -> None:
    entered, release = threading.Event(), threading.Event()

    def hold(request: ToolRequest) -> None:
        entered.set()
        release.wait(timeout=30)

    gateway, _ = open_gateway(world, hooks=Hooks(before_call=hold))
    server = GatewayServer(gateway, "cap-" + "x" * 40)
    port = server.start()
    url = f"http://127.0.0.1:{port}"
    try:
        wrong = GatewayTransport(url, "not-the-capability")
        with pytest.raises(GatewayError) as err:
            wrong._post_sync("/internal/v1/agent/step", {"note": "x"})
        assert err.value.code == "INVALID_CAPABILITY"

        right = GatewayTransport(url, "cap-" + "x" * 40)
        first: list[Any] = []
        worker = threading.Thread(
            target=lambda: first.append(
                right._post_sync(
                    "/internal/v1/tool-calls",
                    req("c1", "invoice.get", {"invoice_id": "INV-1001"}).model_dump(),
                )
            )
        )
        worker.start()
        assert entered.wait(timeout=30)  # call 1 is inside the gateway
        with pytest.raises(GatewayError) as overlap:
            right._post_sync("/internal/v1/agent/step", {"note": "overlap"})
        assert overlap.value.code == "CONCURRENT_CALL_UNSUPPORTED"
        release.set()
        worker.join(timeout=30)
        assert first[0]["status"] == 200
        assert right._post_sync("/internal/v1/agent/step", {"note": "after"}) == {"ok": True}
        with pytest.raises(GatewayError) as missing:
            right._post_sync("/internal/v1/nope", {})
        assert missing.value.code == "NOT_FOUND"
    finally:
        release.set()
        server.stop()


def test_a_fenced_attempt_gets_stale_attempt_from_the_transport(world: World) -> None:
    gateway, claim = open_gateway(world)
    server = GatewayServer(gateway, "c" * 43)
    url = f"http://127.0.0.1:{server.start()}"
    try:
        with world.engine.begin() as conn:
            conn.execute(text("UPDATE jobs SET lease_until = now() - interval '1 second'"))
        assert len(world.queue.reap_expired()) == 1
        transport = GatewayTransport(url, "c" * 43)
        from agentcrash.domain.runtime import AttemptEnded

        with pytest.raises(AttemptEnded) as ended:
            transport._post_sync(
                "/internal/v1/tool-calls",
                req("c1", "invoice.get", {"invoice_id": "INV-1001"}).model_dump(),
            )
        assert ended.value.code == "STALE_ATTEMPT" and server.fenced
        assert world.one("SELECT count(*) FROM events WHERE type = 'tool.requested'") == 0
    finally:
        server.stop()
