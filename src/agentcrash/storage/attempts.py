"""Per-attempt persistence: fenced gateway transactions, evidence, closure, evaluation, cancel.

Every write path enters through ``lock_fenced_attempt`` so a stale worker is rejected before it
reads or writes anything, and cancellation (which locks the same run row) linearizes with writes:
a write either committed before the cancel or is rejected after it.
"""

from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import Connection, Engine, func, select, text, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.exc import ProgrammingError

from agentcrash.domain.canonical import canonical_hash
from agentcrash.domain.events import GENESIS, PendingEvent, StoredEvent, next_hash, project
from agentcrash.domain.evidence import Evidence, Finding, Grade
from agentcrash.domain.models import Budgets, Manifest
from agentcrash.domain.runtime import (
    AttemptCounters,
    AttemptTx,
    EntityRow,
    EnvState,
    RecentCall,
    StoredCall,
    StoredReceipt,
)
from agentcrash.storage import schema as t
from agentcrash.storage.queue import lock_fenced_attempt

TERMINAL_RUN_STATUSES = ("completed", "error")


class PgEnvState:
    def __init__(self, conn: Connection, attempt_id: UUID) -> None:
        self._c = conn
        self._a = attempt_id

    def get(self, entity_type: str, entity_id: str) -> EntityRow | None:
        e = t.environment_rows
        row = self._c.execute(
            select(e.c.version, e.c.data).where(
                e.c.attempt_id == self._a,
                e.c.entity_type == entity_type,
                e.c.entity_id == entity_id,
            )
        ).first()
        return None if row is None else EntityRow(row.version, row.data)

    def cas_update(
        self, entity_type: str, entity_id: str, expected_version: int, data: dict[str, Any]
    ) -> bool:
        e = t.environment_rows
        return (
            self._c.execute(
                update(e)
                .where(
                    e.c.attempt_id == self._a,
                    e.c.entity_type == entity_type,
                    e.c.entity_id == entity_id,
                    e.c.version == expected_version,
                )
                .values(version=expected_version + 1, data=data)
                .returning(e.c.version)
            ).first()
            is not None
        )

    def get_receipt(self, key: str) -> StoredReceipt | None:
        r = t.command_receipts
        row = self._c.execute(
            select(r).where(r.c.attempt_id == self._a, r.c.idempotency_key == key)
        ).first()
        if row is None:
            return None
        return StoredReceipt(row.command_hash, row.semantic_receipt_id, row.result)

    def put_receipt(self, key: str, receipt: StoredReceipt) -> None:
        self._c.execute(
            t.command_receipts.insert().values(
                attempt_id=self._a,
                idempotency_key=key,
                command_hash=receipt.command_hash,
                semantic_receipt_id=receipt.semantic_receipt_id,
                result=receipt.result,
            )
        )

    def insert_ledger_entry(
        self, semantic_entry_id: str, invoice_id: str, data: dict[str, Any]
    ) -> bool:
        return (
            self._c.execute(
                pg_insert(t.ledger_entries)
                .values(
                    attempt_id=self._a,
                    semantic_entry_id=semantic_entry_id,
                    invoice_id=invoice_id,
                    data=data,
                )
                .on_conflict_do_nothing()
                .returning(t.ledger_entries.c.semantic_entry_id)
            ).first()
            is not None
        )

    def ledger_entry_for(self, invoice_id: str) -> dict[str, Any] | None:
        le = t.ledger_entries
        row = self._c.execute(
            select(le.c.data).where(le.c.attempt_id == self._a, le.c.invoice_id == invoice_id)
        ).first()
        return None if row is None else dict(row.data)


def append_event(
    conn: Connection, run_id: UUID, attempt_id: UUID, event: PendingEvent
) -> StoredEvent:
    """Allocate the next contiguous ``seq`` and extend the hash chain. Caller holds the run lock."""
    a = t.run_attempts
    head = conn.execute(select(a.c.next_seq, a.c.chain_head).where(a.c.id == attempt_id)).one()
    prev = head.chain_head or GENESIS
    seq = int(head.next_seq)
    digest = next_hash(
        prev,
        project(
            seq, event.type, event.logical_time_ms, event.actor_id, event.call_id, event.payload
        ),
    )
    conn.execute(
        t.events.insert().values(
            attempt_id=attempt_id,
            seq=seq,
            run_id=run_id,
            type=event.type,
            logical_time_ms=event.logical_time_ms,
            wall_time=event.wall_time,
            actor_id=event.actor_id,
            call_id=event.call_id,
            payload=event.payload,
            prev_hash=prev,
            event_hash=digest,
        )
    )
    conn.execute(update(a).where(a.c.id == attempt_id).values(next_seq=seq + 1, chain_head=digest))
    return StoredEvent(
        seq,
        event.type,
        event.logical_time_ms,
        event.actor_id,
        event.call_id,
        event.payload,
        event.wall_time,
        prev,
        digest,
    )


class PgAttemptTx:
    def __init__(self, conn: Connection, run_id: UUID, attempt_id: UUID) -> None:
        self._c = conn
        self._run = run_id
        self._a = attempt_id
        self.state: EnvState = PgEnvState(conn, attempt_id)

    def cancel_requested(self) -> bool:
        return bool(
            self._c.execute(
                select(t.runs.c.cancel_requested).where(t.runs.c.id == self._run)
            ).scalar_one()
        )

    def counters(self) -> AttemptCounters:
        tc, ev = t.tool_calls, t.events
        ordinals = {
            (r.actor_id, r.tool): int(r.n)
            for r in self._c.execute(
                select(tc.c.actor_id, tc.c.tool, func.count().label("n"))
                .where(tc.c.attempt_id == self._a)
                .group_by(tc.c.actor_id, tc.c.tool)
            )
        }
        steps = self._c.execute(
            select(func.count()).where(ev.c.attempt_id == self._a, ev.c.type == "agent.step")
        ).scalar_one()
        logical = self._c.execute(
            select(func.coalesce(func.max(ev.c.logical_time_ms), 0)).where(
                ev.c.attempt_id == self._a
            )
        ).scalar_one()
        applications: dict[str, int] = {}
        for (payload,) in self._c.execute(
            select(ev.c.payload).where(ev.c.attempt_id == self._a, ev.c.type == "fault.decided")
        ):
            if payload.get("applied") is True:
                rule_id = str(payload["rule_id"])
                applications[rule_id] = applications.get(rule_id, 0) + 1
        return AttemptCounters(
            sum(ordinals.values()), int(steps), int(logical), ordinals, applications
        )

    def get_call(self, call_id: str) -> StoredCall | None:
        tc = t.tool_calls
        row = self._c.execute(
            select(tc).where(tc.c.attempt_id == self._a, tc.c.call_id == call_id)
        ).first()
        if row is None:
            return None
        return StoredCall(
            row.call_id, row.call_index, row.tool_ordinal, row.input_hash, row.outcome
        )

    def recent_calls(self, limit: int) -> list[RecentCall]:
        tc = t.tool_calls
        rows = self._c.execute(
            select(tc.c.tool, tc.c.input_hash, tc.c.outcome)
            .where(tc.c.attempt_id == self._a)
            .order_by(tc.c.call_index.desc())
            .limit(limit)
        )
        return [
            RecentCall(r.tool, r.input_hash, str(r.outcome.get("status", "transport")))
            for r in rows
        ]

    def record_call(
        self,
        call_id: str,
        call_index: int,
        tool_ordinal: int,
        actor_id: str,
        tool: str,
        input_hash: str,
        outcome: dict[str, Any],
    ) -> None:
        self._c.execute(
            t.tool_calls.insert().values(
                attempt_id=self._a,
                call_id=call_id,
                call_index=call_index,
                tool_ordinal=tool_ordinal,
                actor_id=actor_id,
                tool=tool,
                input_hash=input_hash,
                outcome=outcome,
            )
        )

    def append(self, event: PendingEvent) -> StoredEvent:
        return append_event(self._c, self._run, self._a, event)


class PgAttemptGate:
    """Bound to one attempt and fencing token; every ``transaction()`` re-proves the fence."""

    def __init__(self, engine: Engine, attempt_id: UUID, fencing_token: int) -> None:
        self._engine = engine
        self._attempt = attempt_id
        self._token = fencing_token

    @contextmanager
    def transaction(self) -> Iterator[AttemptTx]:
        with self._engine.begin() as conn:
            run_id = lock_fenced_attempt(conn, self._attempt, self._token)
            yield PgAttemptTx(conn, run_id, self._attempt)


def snapshot(conn: Connection, attempt_id: UUID) -> dict[str, Any]:
    """The attempt's whole synthetic world as plain JSON (hashed into closure and evidence)."""
    e, le, r = t.environment_rows, t.ledger_entries, t.command_receipts
    entities: dict[str, dict[str, Any]] = {}
    for row in conn.execute(
        select(e).where(e.c.attempt_id == attempt_id).order_by(e.c.entity_type, e.c.entity_id)
    ):
        entities.setdefault(row.entity_type, {})[row.entity_id] = {
            "version": row.version,
            "data": row.data,
        }
    ledger = [
        row.data
        for row in conn.execute(
            select(le.c.data).where(le.c.attempt_id == attempt_id).order_by(le.c.semantic_entry_id)
        )
    ]
    receipts = [
        {
            "idempotency_key": row.idempotency_key,
            "command_hash": row.command_hash,
            "semantic_receipt_id": row.semantic_receipt_id,
            "result": row.result,
        }
        for row in conn.execute(
            select(r).where(r.c.attempt_id == attempt_id).order_by(r.c.idempotency_key)
        )
    ]
    return {"entities": entities, "ledger_entries": ledger, "receipts": receipts}


class AttemptRepo:
    def __init__(self, engine: Engine) -> None:
        self._engine = engine

    def schema_revision(self) -> str | None:
        with self._engine.connect() as conn:
            try:
                return conn.execute(text("SELECT version_num FROM alembic_version")).scalar()
            except ProgrammingError:  # never migrated
                return None

    def gate(self, attempt_id: UUID, fencing_token: int) -> PgAttemptGate:
        return PgAttemptGate(self._engine, attempt_id, fencing_token)

    def manifest(self, run_id: UUID) -> Manifest:
        with self._engine.connect() as conn:
            doc = conn.execute(select(t.runs.c.manifest).where(t.runs.c.id == run_id)).scalar_one()
        return Manifest.model_validate(doc)

    def cancel_requested(self, run_id: UUID) -> bool:
        with self._engine.connect() as conn:
            return bool(
                conn.execute(
                    select(t.runs.c.cancel_requested).where(t.runs.c.id == run_id)
                ).scalar_one()
            )

    def start_attempt(
        self,
        attempt_id: UUID,
        fencing_token: int,
        rows: list[tuple[str, str, int, dict[str, Any]]],
        started: PendingEvent,
    ) -> None:
        """Clone the fixture into fresh per-attempt rows, record the initial snapshot and emit
        ``attempt.started`` in one fenced transaction. Nothing is shared with other attempts."""
        with self._engine.begin() as conn:
            run_id = lock_fenced_attempt(conn, attempt_id, fencing_token)
            conn.execute(
                t.environment_rows.insert(),
                [
                    {
                        "attempt_id": attempt_id,
                        "entity_type": kind,
                        "entity_id": key,
                        "version": version,
                        "data": data,
                    }
                    for kind, key, version, data in rows
                ],
            )
            conn.execute(
                update(t.run_attempts)
                .where(t.run_attempts.c.id == attempt_id)
                .values(initial_state=snapshot(conn, attempt_id))
            )
            append_event(conn, run_id, attempt_id, started)

    def finalize(
        self,
        attempt_id: UUID,
        fencing_token: int,
        termination: str,
        terminal_result: dict[str, Any] | None,
        now: datetime,
        *,
        run_error: str | None = None,
    ) -> None:
        """Close the attempt once: closure event, final snapshot and hashes, job done, and the
        grading job (or run ``error`` when ``run_error`` is set), atomically under the fence."""
        a, r, j = t.run_attempts, t.runs, t.jobs
        with self._engine.begin() as conn:
            run_id = lock_fenced_attempt(conn, attempt_id, fencing_token)
            final = snapshot(conn, attempt_id)
            final_hash = canonical_hash(final)
            head = conn.execute(
                select(a.c.next_seq, a.c.chain_head).where(a.c.id == attempt_id)
            ).one()
            logical = conn.execute(
                select(func.coalesce(func.max(t.events.c.logical_time_ms), 0)).where(
                    t.events.c.attempt_id == attempt_id
                )
            ).scalar_one()
            closed = append_event(
                conn,
                run_id,
                attempt_id,
                PendingEvent(
                    "attempt.closed",
                    int(logical),
                    None,
                    None,
                    {
                        "termination": termination,
                        "final_state_hash": final_hash,
                        "terminal_result_hash": canonical_hash(terminal_result or {}),
                        "event_count": int(head.next_seq),  # events so far + this closure
                        "preclose_chain_head": head.chain_head or GENESIS,
                    },
                    now,
                ),
            )
            conn.execute(
                update(a)
                .where(a.c.id == attempt_id)
                .values(
                    termination=termination,
                    final_state=final,
                    final_state_hash=final_hash,
                    chain_head=closed.event_hash,
                    closed_at=func.now(),
                )
            )
            conn.execute(
                update(j)
                .where(j.c.attempt_id == attempt_id, j.c.kind == "execute")
                .values(status="error" if run_error else "done")
            )
            if run_error:
                conn.execute(
                    update(r)
                    .where(r.c.id == run_id)
                    .values(status="error", error_code=run_error, closed_at=func.now())
                )
                return
            conn.execute(
                update(r)
                .where(r.c.id == run_id, r.c.status == "running")
                .values(status="evaluating")
            )
            conn.execute(
                pg_insert(j)
                .values(
                    id=uuid4(),
                    kind="grade",
                    run_id=run_id,
                    attempt_id=attempt_id,
                    dedupe_key=f"grade:{attempt_id}",
                )
                .on_conflict_do_nothing(index_elements=["dedupe_key"])
            )

    # --- evaluation ----------------------------------------------------------------------

    def evidence(self, attempt_id: UUID, budgets: Budgets, params: dict[str, Any]) -> Evidence:
        a, ev, tc = t.run_attempts, t.events, t.tool_calls
        with self._engine.connect() as conn:
            att = conn.execute(select(a).where(a.c.id == attempt_id)).one()
            events = [
                StoredEvent(
                    r.seq,
                    r.type,
                    r.logical_time_ms,
                    r.actor_id,
                    r.call_id,
                    r.payload,
                    r.wall_time,
                    r.prev_hash,
                    r.event_hash,
                )
                for r in conn.execute(
                    select(ev).where(ev.c.attempt_id == attempt_id).order_by(ev.c.seq)
                )
            ]
            calls = [
                StoredCall(r.call_id, r.call_index, r.tool_ordinal, r.input_hash, r.outcome)
                for r in conn.execute(
                    select(tc).where(tc.c.attempt_id == attempt_id).order_by(tc.c.call_index)
                )
            ]
        return Evidence(
            attempt_id=attempt_id,
            termination=att.termination,
            closed=att.closed_at is not None,
            events=events,
            tool_calls=calls,
            initial_state=att.initial_state,
            final_state=att.final_state,
            final_state_hash=att.final_state_hash,
            chain_head=att.chain_head,
            budgets=budgets,
            params=params,
        )

    def store_evaluation(
        self,
        run_id: UUID,
        attempt_id: UUID,
        grader_set_hash: str,
        snapshot_hash: str | None,
        verdict: str,
        grades: list[Grade],
        job_id: UUID,
        primary_reason: str | None = None,
        findings: list[Finding] | None = None,
    ) -> None:
        """Publish the evaluation exactly once per ``(attempt, grader set)`` and finish the run.
        A duplicate delivery inserts nothing and changes nothing."""
        r, j = t.runs, t.jobs
        with self._engine.begin() as conn:
            conn.execute(select(r.c.id).where(r.c.id == run_id).with_for_update())
            inserted = conn.execute(
                pg_insert(t.evaluations)
                .values(
                    id=uuid4(),
                    attempt_id=attempt_id,
                    grader_set_hash=grader_set_hash,
                    snapshot_hash=snapshot_hash,
                    verdict=verdict,
                    grades=[g.model_dump(mode="json") for g in grades],
                    primary_reason=primary_reason,
                    findings=[f.model_dump(mode="json") for f in findings or []],
                )
                .on_conflict_do_nothing(index_elements=["attempt_id", "grader_set_hash"])
                .returning(t.evaluations.c.id)
            ).first()
            if inserted is not None:
                conn.execute(
                    update(r)
                    .where(r.c.id == run_id, r.c.status == "evaluating")
                    .values(
                        status="completed",
                        verdict=verdict,
                        selected_attempt_id=attempt_id,
                        closed_at=func.now(),
                    )
                )
            conn.execute(update(j).where(j.c.id == job_id).values(status="done"))
