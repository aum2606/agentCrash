"""Exports and strict replays in PostgreSQL (CONTRACTS.md sections 3 and 8, ADR-012).

``ExportStoreMixin`` is the API side: validate, claim the idempotency key, and create the export
or replay row together with its job in one transaction. ``ExportRepo`` is the worker side:
publish the artifact metadata or the replay result and finish the job. Neither writes any run,
attempt, event or evaluation row, so a replay cannot change the evidence it replays.
"""

from collections.abc import Callable
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import Connection, Engine, func, literal_column, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert

from agentcrash.domain.models import Manifest
from agentcrash.domain.ports import (
    AttemptNotClosed,
    AttemptNotInRun,
    CreateExportResult,
    CreateReplayResult,
    ExportRecord,
    IdempotencyConflict,
    IdempotencyExpired,
    ReplayIncompatible,
    ReplayRecord,
)
from agentcrash.storage import schema as t


def _claim(
    conn: Connection,
    principal: str,
    route: str,
    key: str,
    request_hash: str,
    resource_type: str,
    resource_id: UUID,
) -> UUID | None:
    """Claim the idempotency key. None: newly claimed. Otherwise the earlier resource's id."""
    claimed = conn.execute(
        pg_insert(t.api_idempotency)
        .values(
            principal=principal,
            route=route,
            key=key,
            request_hash=request_hash,
            resource_type=resource_type,
            resource_id=resource_id,
        )
        .on_conflict_do_nothing()
        .returning(t.api_idempotency.c.resource_id)
    ).first()
    if claimed is not None:
        return None
    prior = conn.execute(
        select(t.api_idempotency).where(
            t.api_idempotency.c.principal == principal,
            t.api_idempotency.c.route == route,
            t.api_idempotency.c.key == key,
        )
    ).one()
    if prior.tombstoned_at is not None:
        raise IdempotencyExpired
    if prior.request_hash != request_hash:
        raise IdempotencyConflict
    return prior.resource_id  # type: ignore[no-any-return]


def _closed_attempt(conn: Connection, run_id: UUID, attempt_id: UUID) -> None:
    row = conn.execute(
        select(t.run_attempts.c.run_id, t.run_attempts.c.closed_at).where(
            t.run_attempts.c.id == attempt_id
        )
    ).first()
    if row is None or row.run_id != run_id:
        raise AttemptNotInRun
    if row.closed_at is None:
        raise AttemptNotClosed


def _export(row: Any) -> ExportRecord:
    return ExportRecord(
        id=row.id,
        run_id=row.run_id,
        attempt_id=row.attempt_id,
        status=row.status,
        pin=row.pin,
        artifact_hash=row.artifact_hash,
        error_code=row.error_code,
        created_at=row.created_at,
        closed_at=row.closed_at,
    )


def _replay(row: Any) -> ReplayRecord:
    return ReplayRecord(
        id=row.id,
        run_id=row.run_id,
        attempt_id=row.source_attempt_id,
        status=row.status,
        adapter_hash=row.adapter_hash,
        consumed_events=row.consumed_events,
        total_events=row.total_events,
        divergence=row.divergence,
        error_code=row.error_code,
        created_at=row.created_at,
        closed_at=row.closed_at,
    )


class ExportStoreMixin:
    _engine: Engine

    def create_export(
        self,
        *,
        principal: str,
        route: str,
        key: str,
        request_hash: str,
        run_id: UUID,
        attempt_id: UUID,
        pin: bool,
    ) -> CreateExportResult | None:
        export_id = uuid4()
        with self._engine.begin() as conn:
            if conn.execute(select(t.runs.c.id).where(t.runs.c.id == run_id)).first() is None:
                return None  # nothing written yet
            prior = _claim(conn, principal, route, key, request_hash, "export", export_id)
            if prior is not None:
                row = conn.execute(select(t.exports).where(t.exports.c.id == prior)).one()
                return CreateExportResult(export=_export(row), replayed=True)
            _closed_attempt(conn, run_id, attempt_id)  # raising rolls back the key claim too
            conn.execute(
                t.exports.insert().values(
                    id=export_id, run_id=run_id, attempt_id=attempt_id, pin=pin
                )
            )
            conn.execute(
                t.jobs.insert().values(
                    id=uuid4(),
                    kind="export",
                    run_id=run_id,
                    attempt_id=attempt_id,
                    dedupe_key=f"export:{export_id}",
                    subject_id=export_id,
                )
            )
            row = conn.execute(select(t.exports).where(t.exports.c.id == export_id)).one()
            return CreateExportResult(export=_export(row), replayed=False)

    def get_export(self, export_id: UUID) -> ExportRecord | None:
        with self._engine.connect() as conn:
            row = conn.execute(select(t.exports).where(t.exports.c.id == export_id)).first()
        return None if row is None else _export(row)

    def create_replay(
        self,
        *,
        principal: str,
        route: str,
        key: str,
        request_hash: str,
        run_id: UUID,
        attempt_id: UUID,
        check: Callable[[Manifest], list[str]],
    ) -> CreateReplayResult | None:
        replay_id = uuid4()
        with self._engine.begin() as conn:
            document = conn.execute(select(t.runs.c.manifest).where(t.runs.c.id == run_id)).scalar()
            if document is None:
                return None
            prior = _claim(conn, principal, route, key, request_hash, "replay", replay_id)
            if prior is not None:
                row = conn.execute(select(t.replays).where(t.replays.c.id == prior)).one()
                return CreateReplayResult(replay=_replay(row), replayed=True)
            _closed_attempt(conn, run_id, attempt_id)
            reasons = check(Manifest.model_validate(document))
            if reasons:
                raise ReplayIncompatible(reasons)
            conn.execute(
                t.replays.insert().values(id=replay_id, run_id=run_id, source_attempt_id=attempt_id)
            )
            conn.execute(
                t.jobs.insert().values(
                    id=uuid4(),
                    kind="replay",
                    run_id=run_id,
                    attempt_id=attempt_id,
                    dedupe_key=f"replay:{replay_id}",
                    subject_id=replay_id,
                )
            )
            row = conn.execute(select(t.replays).where(t.replays.c.id == replay_id)).one()
            return CreateReplayResult(replay=_replay(row), replayed=False)

    def get_replay(self, replay_id: UUID) -> ReplayRecord | None:
        with self._engine.connect() as conn:
            row = conn.execute(select(t.replays).where(t.replays.c.id == replay_id)).first()
        return None if row is None else _replay(row)


class ExportRepo:
    """Worker side. Each method is one transaction that also finishes the job, and only if the
    worker still holds its lease (a lost lease means another delivery owns the result)."""

    def __init__(self, engine: Engine) -> None:
        self._engine = engine

    def evaluations(self, attempt_id: UUID) -> list[dict[str, Any]]:
        e = t.evaluations
        with self._engine.connect() as conn:
            rows = conn.execute(
                select(e).where(e.c.attempt_id == attempt_id).order_by(e.c.evaluated_at, e.c.id)
            ).all()
        return [
            {
                "id": str(r.id),
                "grader_set_hash": r.grader_set_hash,
                "snapshot_hash": r.snapshot_hash,
                "verdict": r.verdict,
                "grades": r.grades,
                "primary_reason": r.primary_reason,
                "findings": r.findings,
                "evaluated_at": r.evaluated_at.isoformat(),
            }
            for r in rows
        ]

    def _finish_job(self, conn: Connection, job_id: UUID, status: str) -> bool:
        done = conn.execute(
            update(t.jobs)
            .where(t.jobs.c.id == job_id, t.jobs.c.status == "leased")
            .values(status=status, lease_until=None)
        )
        return done.rowcount == 1

    def complete_export(
        self, job_id: UUID, export_id: UUID, sha256: str, size: int, storage_key: str
    ) -> bool:
        with self._engine.begin() as conn:
            if not self._finish_job(conn, job_id, "done"):
                return False
            conn.execute(
                pg_insert(t.artifacts)
                .values(
                    hash=sha256, kind="bundle", byte_size=size, relative_storage_key=storage_key
                )
                .on_conflict_do_nothing()
            )
            pin = conn.execute(select(t.exports.c.pin).where(t.exports.c.id == export_id)).scalar()
            conn.execute(
                pg_insert(t.artifact_refs)
                .values(
                    owner_type="export",
                    owner_id=export_id,
                    hash=sha256,
                    pin_until=literal_column("'infinity'::timestamptz") if pin else None,
                )
                .on_conflict_do_nothing()
            )
            conn.execute(
                update(t.exports)
                .where(t.exports.c.id == export_id)
                .values(status="completed", artifact_hash=sha256, closed_at=func.now())
            )
        return True

    def fail_export(self, job_id: UUID, export_id: UUID, code: str) -> bool:
        with self._engine.begin() as conn:
            if not self._finish_job(conn, job_id, "error"):
                return False
            conn.execute(
                update(t.exports)
                .where(t.exports.c.id == export_id)
                .values(status="error", error_code=code, closed_at=func.now())
            )
        return True

    def complete_replay(
        self,
        job_id: UUID,
        replay_id: UUID,
        adapter_hash: str,
        consumed: int,
        total: int,
        divergence: dict[str, Any] | None,
    ) -> bool:
        with self._engine.begin() as conn:
            if not self._finish_job(conn, job_id, "done"):
                return False
            conn.execute(
                update(t.replays)
                .where(t.replays.c.id == replay_id)
                .values(
                    status="completed",
                    adapter_hash=adapter_hash,
                    consumed_events=consumed,
                    total_events=total,
                    divergence=divergence,
                    closed_at=func.now(),
                )
            )
        return True

    def fail_replay(self, job_id: UUID, replay_id: UUID, code: str) -> bool:
        with self._engine.begin() as conn:
            if not self._finish_job(conn, job_id, "error"):
                return False
            conn.execute(
                update(t.replays)
                .where(t.replays.c.id == replay_id)
                .values(status="error", error_code=code, closed_at=func.now())
            )
        return True
