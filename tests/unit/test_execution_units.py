"""Pure pieces of the execution stack: the hash chain, redaction, invoice tool semantics over an
in-memory state, the seeded RNG, the naive agent against a scripted client, and the isolation of
the child process. No database, no network."""

import asyncio
import hashlib
import json
import subprocess
import sys
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from agentcrash.agents.naive import NaiveAgent
from agentcrash.agents.rng import SeededRng
from agentcrash.domain.events import (
    GENESIS,
    MAX_EVENT_BYTES,
    EventTooLarge,
    StoredEvent,
    chain_problem,
    next_hash,
    observable_digest,
    project,
)
from agentcrash.domain.redaction import REDACTED, redact
from agentcrash.domain.runtime import (
    AgentTask,
    EntityRow,
    StoredReceipt,
    ToolMeta,
    ToolRequest,
    ToolResponse,
)
from agentcrash.environments.invoice import tools
from agentcrash.environments.invoice.fixture import load_bundle
from agentcrash.workers.supervisor import AttemptSupervisor

ROOT = Path(__file__).resolve().parents[2]


# --- hash chain ---------------------------------------------------------------------------


def test_genesis_is_the_documented_constant() -> None:
    assert hashlib.sha256(b"agentcrash-events-v1").hexdigest() == GENESIS


def test_chain_hash_has_a_known_vector() -> None:
    event = project(1, "attempt.started", 0, None, None, {})
    body = (
        b'{"actor_id":null,"call_id":null,"logical_time_ms":0,"payload":{},"seq":1,'
        b'"type":"attempt.started"}'
    )
    assert next_hash(GENESIS, event) == hashlib.sha256(bytes.fromhex(GENESIS) + body).hexdigest()


def make_events(n: int) -> list[StoredEvent]:
    out, head = [], GENESIS
    for seq in range(1, n + 1):
        digest = next_hash(head, project(seq, "agent.step", seq, "agent", None, {"step": seq}))
        out.append(
            StoredEvent(
                seq,
                "agent.step",
                seq,
                "agent",
                None,
                {"step": seq},
                datetime(2026, 1, 1, tzinfo=UTC),
                head,
                digest,
            )
        )
        head = digest
    return out


def test_an_intact_chain_has_no_problem_and_a_stable_digest() -> None:
    events = make_events(5)
    assert chain_problem(events) is None
    assert observable_digest(events) == events[-1].event_hash == observable_digest(make_events(5))


def test_wall_time_and_storage_envelope_do_not_affect_the_digest() -> None:
    events = make_events(3)
    later = [replace(e, wall_time=datetime(2030, 1, 1, tzinfo=UTC)) for e in events]
    assert observable_digest(later) == observable_digest(events)


@pytest.mark.parametrize("position", [0, 2, 4])
def test_editing_any_event_breaks_the_chain(position: int) -> None:
    events = make_events(5)
    events[position] = replace(events[position], payload={"step": 99})
    assert chain_problem(events) is not None


def test_dropping_reordering_or_relinking_events_is_detected() -> None:
    events = make_events(4)
    assert "EVENT_GAP" in str(chain_problem(events[:1] + events[2:]))
    assert chain_problem([events[1], events[0], *events[2:]]) is not None
    relinked = [replace(events[0], prev_hash="f" * 64), *events[1:]]
    assert "BROKEN_LINK" in str(chain_problem(relinked))


def test_oversized_events_are_rejected_not_truncated() -> None:
    huge = project(1, "agent.step", 0, None, None, {"x": "a" * MAX_EVENT_BYTES})
    with pytest.raises(EventTooLarge):
        next_hash(GENESIS, huge)


# --- redaction ----------------------------------------------------------------------------


def test_credential_keys_and_secret_looking_values_are_redacted_everywhere() -> None:
    value = {
        "ok": "fine",
        "Authorization": "Bearer abcdefgh12345678",
        "nested": [{"api_key": "x"}, {"note": "use sk-ABCDEFGHIJKLMNOPQRSTUV now"}],
        "aws": "AKIAABCDEFGHIJKLMNOP",
        "pem": "-----BEGIN RSA PRIVATE KEY-----",
        "count": 3,
    }
    out = json.dumps(redact(value))
    for canary in ("abcdefgh12345678", "ABCDEFGHIJKLMNOPQRSTUV", "AKIAABCDEFGHIJKLMNOP", "BEGIN"):
        assert canary not in out
    assert redact(value)["ok"] == "fine" and redact(value)["count"] == 3
    assert redact({"token": "t"}) == {"token": REDACTED}


def test_redaction_does_not_mutate_its_input() -> None:
    original = {"password": "p", "list": [{"secret": "s"}]}
    redact(original)
    assert original == {"password": "p", "list": [{"secret": "s"}]}


# --- invoice tools over an in-memory EnvState ----------------------------------------------


class MemState:
    def __init__(self) -> None:
        bundle = load_bundle(ROOT / "fixtures/invoice/v1/standard.json")
        self.rows = {(k, i): EntityRow(v, d) for k, i, v, d in bundle.rows()}
        self.receipts: dict[str, StoredReceipt] = {}
        self.ledger: dict[str, dict[str, Any]] = {}

    def get(self, kind: str, key: str) -> EntityRow | None:
        return self.rows.get((kind, key))

    def cas_update(self, kind: str, key: str, expected: int, data: dict[str, Any]) -> bool:
        row = self.rows.get((kind, key))
        if row is None or row.version != expected:
            return False
        self.rows[(kind, key)] = EntityRow(expected + 1, data)
        return True

    def get_receipt(self, key: str) -> StoredReceipt | None:
        return self.receipts.get(key)

    def put_receipt(self, key: str, receipt: StoredReceipt) -> None:
        self.receipts[key] = receipt

    def insert_ledger_entry(self, entry_id: str, invoice_id: str, data: dict[str, Any]) -> bool:
        if invoice_id in self.ledger:
            return False
        self.ledger[invoice_id] = data
        return True

    def ledger_entry_for(self, invoice_id: str) -> dict[str, Any] | None:
        return self.ledger.get(invoice_id)


POST = {
    "invoice_id": "INV-1001",
    "po_id": "PO-2001",
    "amount_minor": 10000,
    "currency": "USD",
    "expected_invoice_version": 1,
    "source_po_version": 2,
}


def test_reads_return_the_fixture_with_versions() -> None:
    s = MemState()
    inv = tools.dispatch("invoice.get", s, {"invoice_id": "INV-1001"}, None)
    po = tools.dispatch("purchase_order.get", s, {"po_id": "PO-2001"}, None)
    doc = tools.dispatch("document.get", s, {"invoice_id": "INV-1001"}, None)
    assert (inv.status, inv.body["amount_minor"], inv.source_version) == (200, 10000, 1)
    assert po.body["provenance"] == {"snapshot": "current", "version": 2}
    assert doc.status == 200 and "net 30" in doc.body["text"]
    missing = tools.dispatch("invoice.get", s, {"invoice_id": "INV-0"}, None)
    assert (missing.status, missing.body["error"]["code"]) == (404, "NOT_FOUND")


def test_posting_commits_once_and_bumps_the_invoice_version() -> None:
    s = MemState()
    out = tools.dispatch("ledger.post_invoice", s, POST, "post:INV-1001")
    assert out.status == 200 and out.body["invoice_version"] == 2 and out.mutation is not None
    assert s.rows[("invoice", "INV-1001")].data["status"] == "posted"
    assert out.body["receipt_id"] == "receipt:post:INV-1001"  # deterministic, not a UUID
    replay = tools.dispatch("ledger.post_invoice", s, POST, "post:INV-1001")
    assert replay.status == 200 and replay.body == out.body and replay.mutation is None
    assert s.rows[("invoice", "INV-1001")].version == 2


def test_the_environment_does_not_enforce_the_graders_freshness_oracle() -> None:
    """A stale source version posts successfully so the grader can catch it (CONTRACTS 6)."""
    s = MemState()
    stale = {**POST, "source_po_version": 1, "amount_minor": 9000}
    assert tools.dispatch("ledger.post_invoice", s, stale, "k").status == 200
    assert s.ledger["INV-1001"]["source_po_version"] == 1


def test_receipt_lookup_reports_missing_commands() -> None:
    s = MemState()
    assert tools.dispatch("receipt.get", s, {"idempotency_key": "nope"}, None).status == 404
    tools.dispatch("ledger.post_invoice", s, POST, "k1")
    found = tools.dispatch("receipt.get", s, {"idempotency_key": "k1"}, None)
    assert found.status == 200 and found.body["status"] == "committed"


# --- deterministic agent randomness ---------------------------------------------------------


def test_agent_rng_is_reproducible_independent_and_bounded() -> None:
    a, b = SeededRng("42", "invoice-resilient"), SeededRng("42", "invoice-resilient")
    seq = [a.jitter_ms(100) for _ in range(20)]
    assert seq == [b.jitter_ms(100) for _ in range(20)]
    assert all(0 <= x <= 100 for x in seq)
    assert seq != [SeededRng("43", "invoice-resilient").jitter_ms(100) for _ in range(20)]
    assert SeededRng("1", "x").jitter_ms(0) == 0


# --- the naive agent against a scripted gateway ---------------------------------------------


class Scripted:
    def __init__(self, status: dict[str, int] | None = None) -> None:
        self.state = MemState()
        self.status = status or {}
        self.calls: list[ToolRequest] = []
        self.steps: list[str] = []

    async def call(self, request: ToolRequest) -> ToolResponse:
        self.calls.append(request)
        out = tools.dispatch(request.tool, self.state, request.args, request.idempotency_key)
        status = self.status.get(request.tool, out.status)
        return ToolResponse(
            status=status,
            body=out.body if status == out.status else {"error": {}},
            meta=ToolMeta(
                call_index=len(self.calls),
                tool_ordinal=1,
                logical_time_ms=0,
                source_version=out.source_version,
            ),
        )

    async def step(self, note: str) -> None:
        self.steps.append(note)


TASK = AgentTask(
    task_id="post-invoice",
    instruction="x",
    invoice_id="INV-1001",
    max_source_age_ms=3_600_000,
    logical_epoch="2026-01-01T00:00:00Z",
    seed="42",
)


def run_agent(client: Scripted) -> Any:
    agent = NaiveAgent()
    return asyncio.run(agent.run(TASK, client, None, SeededRng("42", "n")))  # type: ignore[arg-type]


def test_the_naive_agent_posts_exactly_once_with_a_stable_semantic_key() -> None:
    client = Scripted()
    result = run_agent(client)
    assert result.status == "completed"
    assert [c.tool for c in client.calls] == [
        "invoice.get",
        "document.get",
        "purchase_order.get",
        "ledger.post_invoice",
    ]
    assert [c.call_id for c in client.calls] == ["call-1", "call-2", "call-3", "call-4"]
    assert client.calls[-1].idempotency_key == "post:INV-1001"
    assert len(client.state.ledger) == 1


def test_the_naive_agent_does_not_retry_when_a_read_fails() -> None:
    client = Scripted({"purchase_order.get": 503})
    result = run_agent(client)
    assert result.status == "gave_up" and len(client.state.ledger) == 0
    assert [c.tool for c in client.calls].count("purchase_order.get") == 1


# --- process isolation ----------------------------------------------------------------------


def test_the_child_environment_is_an_allowlist_and_carries_no_secret(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("AGENTCRASH_DATABASE_URL", "postgresql://u:SECRETPW@h/db")
    monkeypatch.setenv("POSTGRES_PASSWORD", "SECRETPW")
    monkeypatch.setenv("OPENAI_API_KEY", "sk-canary")
    sup = AttemptSupervisor.__new__(AttemptSupervisor)
    env = AttemptSupervisor._child_env(sup)
    assert "SECRETPW" not in json.dumps(env) and "sk-canary" not in json.dumps(env)
    allowed = {"SYSTEMROOT", "SYSTEMDRIVE", "TEMP", "TMP", "LANG", "LC_ALL", "PYTHONIOENCODING"}
    assert set(env) <= allowed


def test_the_child_module_loads_no_storage_or_database_code() -> None:
    code = (
        "import sys, agentcrash.execution.child\n"
        "heavy = ('sqlalchemy', 'psycopg', 'alembic', 'fastapi', 'uvicorn')\n"
        "inner = ('agentcrash.storage', 'agentcrash.workers', 'agentcrash.api',\n"
        "         'agentcrash.evaluation')\n"
        "bad = [m for m in sys.modules\n"
        "       if m.split('.')[0] in heavy or m.startswith(inner)]\n"
        "print(bad)\n"
    )
    out = subprocess.run(  # noqa: S603
        [sys.executable, "-I", "-c", code], capture_output=True, text=True, timeout=60, check=True
    )
    assert out.stdout.strip() == "[]", out.stdout


def _run_child(stdin: str) -> int:
    return subprocess.run(  # noqa: S603
        [sys.executable, "-I", "-m", "agentcrash.execution.child"],
        input=stdin,
        text=True,
        capture_output=True,
        timeout=60,
        check=False,
    ).returncode


def test_the_child_rejects_garbage_input() -> None:
    assert _run_child("not json\n") == 2


def test_the_child_never_runs_an_adapter_that_is_not_allowlisted() -> None:
    spec = {
        "url": "http://127.0.0.1:1",
        "capability": "c",
        "agent_id": "evil",
        "agent_version": "9",
        "task": {},
    }
    assert _run_child(json.dumps(spec) + "\n") == 2
