"""PostgreSQL implementation of ``domain.ports.Store`` (synchronous SQLAlchemy Core).

Each public method is one transaction. Run creation writes the idempotency record, the run and
its execute job atomically; nothing here (or anywhere on the request path) executes an agent.
"""

from datetime import datetime
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import Connection, Engine, func, select, text, tuple_, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.exc import ProgrammingError

from agentcrash.domain.evidence import Finding, Grade
from agentcrash.domain.models import (
    AgentRef,
    CompiledScenario,
    Manifest,
    RunCreate,
    Scenario,
    ScenarioRef,
)
from agentcrash.domain.pagination import InvalidCursor, decode_cursor, encode_cursor
from agentcrash.domain.ports import (
    AgentIdentity,
    AttemptNotInRun,
    AttemptSummary,
    CancelResult,
    CreateRunResult,
    EvaluationRecord,
    EventPage,
    EventRecord,
    IdempotencyConflict,
    IdempotencyExpired,
    PutScenarioResult,
    QueueFull,
    RunPage,
    RunRecord,
    ScenarioConflict,
    ScenarioPage,
    ScenarioSummary,
    StoredScenario,
)
from agentcrash.storage import schema as t
from agentcrash.storage.exports import ExportStoreMixin

# Serializes the "count queued, then insert" admission check across transactions.
_ADMISSION_LOCK_KEY = 0x4143_4C5F_4144_4D31  # "ACL_ADM1"


def _summary(row: Any) -> ScenarioSummary:
    return ScenarioSummary(
        id=row.id,
        version=row.version,
        content_hash=row.content_hash,
        description=row.normalized_json.get("description", ""),
        created_at=row.created_at,
    )


class PostgresStore(ExportStoreMixin):
    def __init__(self, engine: Engine) -> None:
        self._engine = engine

    # --- health ---------------------------------------------------------------------------

    def ping(self) -> None:
        with self._engine.connect() as conn:
            conn.execute(text("SELECT 1"))

    def schema_revision(self) -> str | None:
        with self._engine.connect() as conn:
            try:
                return conn.execute(text("SELECT version_num FROM alembic_version")).scalar()
            except ProgrammingError:  # table missing: database was never migrated
                return None

    # --- scenarios ------------------------------------------------------------------------

    def put_scenario(self, compiled: CompiledScenario) -> PutScenarioResult:
        sc = compiled.scenario
        with self._engine.begin() as conn:
            inserted = conn.execute(
                pg_insert(t.scenario_versions)
                .values(
                    id=sc.id,
                    version=sc.version,
                    content_hash=compiled.content_hash,
                    schema_version=sc.schema_version,
                    normalized_json=sc.model_dump(mode="json"),
                )
                .on_conflict_do_nothing(index_elements=["id", "version"])
                .returning(t.scenario_versions)
            ).first()
            if inserted is not None:
                return PutScenarioResult(scenario=_summary(inserted), created=True)
            existing = conn.execute(
                select(t.scenario_versions).where(
                    t.scenario_versions.c.id == sc.id, t.scenario_versions.c.version == sc.version
                )
            ).one()
            if existing.content_hash != compiled.content_hash:
                raise ScenarioConflict
            return PutScenarioResult(scenario=_summary(existing), created=False)

    def get_scenario(self, scenario_id: str, version: int) -> StoredScenario | None:
        with self._engine.connect() as conn:
            row = conn.execute(
                select(t.scenario_versions).where(
                    t.scenario_versions.c.id == scenario_id,
                    t.scenario_versions.c.version == version,
                )
            ).first()
        if row is None:
            return None
        return StoredScenario(
            **_summary(row).model_dump(), scenario=Scenario.model_validate(row.normalized_json)
        )

    def list_scenarios(self, cursor: str | None, limit: int) -> ScenarioPage:
        sv = t.scenario_versions
        query = select(sv).order_by(sv.c.created_at.desc(), sv.c.id.desc(), sv.c.version.desc())
        if cursor is not None:
            created_at, key = decode_cursor(cursor, 2)
            try:
                version = int(key[1])
            except ValueError:
                raise InvalidCursor from None
            query = query.where(
                tuple_(sv.c.created_at, sv.c.id, sv.c.version) < tuple_(created_at, key[0], version)
            )
        with self._engine.connect() as conn:
            rows = conn.execute(query.limit(limit + 1)).all()
        items = [_summary(r) for r in rows[:limit]]
        more = len(rows) > limit
        last = items[-1] if more else None
        return ScenarioPage(
            items=items,
            next_cursor=encode_cursor(last.created_at, [last.id, str(last.version)])
            if last
            else None,
        )

    # --- agents ---------------------------------------------------------------------------

    def ensure_agent_version(
        self,
        agent_id: str,
        version: str,
        adapter_registry_key: str,
        config_schema_hash: str,
        source_hash: str | None,
    ) -> None:
        with self._engine.begin() as conn:
            conn.execute(
                pg_insert(t.agent_versions)
                .values(
                    id=agent_id,
                    version=version,
                    adapter_registry_key=adapter_registry_key,
                    config_schema_hash=config_schema_hash,
                    source_hash=source_hash,
                )
                .on_conflict_do_nothing(index_elements=["id", "version"])
            )

    # --- runs -----------------------------------------------------------------------------

    def create_run(
        self,
        *,
        principal: str,
        route: str,
        key: str,
        request_hash: str,
        run: RunCreate,
        manifest: Manifest,
        manifest_hash: str,
        queue_cap: int,
        parent_run_id: UUID | None = None,
    ) -> CreateRunResult:
        run_id = uuid4()
        with self._engine.begin() as conn:
            # 1. Claim the idempotency key. A concurrent transaction using the same key blocks
            #    on the unique index until we commit or roll back, then sees our row.
            claimed = conn.execute(
                pg_insert(t.api_idempotency)
                .values(
                    principal=principal,
                    route=route,
                    key=key,
                    request_hash=request_hash,
                    resource_type="run",
                    resource_id=run_id,
                )
                .on_conflict_do_nothing()
                .returning(t.api_idempotency.c.resource_id)
            ).first()
            if claimed is None:
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
                existing = self._load_runs(conn, [prior.resource_id])
                return CreateRunResult(run=existing[0], replayed=True)

            # 2. Admission: count and insert under one lock so the cap cannot be overshot.
            #    Rolling back here also removes the idempotency claim, so a retry is fresh.
            conn.execute(text("SELECT pg_advisory_xact_lock(:k)"), {"k": _ADMISSION_LOCK_KEY})
            queued = conn.execute(
                select(func.count()).select_from(t.runs).where(t.runs.c.status == "queued")
            ).scalar_one()
            if queued >= queue_cap:
                raise QueueFull

            # 3. Run + job in the same transaction as the idempotency record.
            conn.execute(
                t.runs.insert().values(
                    id=run_id,
                    scenario_id=run.scenario.id,
                    scenario_version=run.scenario.version,
                    agent_id=run.agent.id,
                    agent_version=run.agent.version,
                    seed=int(run.seed),
                    manifest=manifest.model_dump(mode="json"),
                    manifest_hash=manifest_hash,
                    labels=run.labels,
                    parent_run_id=parent_run_id,
                )
            )
            conn.execute(
                t.jobs.insert().values(
                    id=uuid4(), kind="execute", run_id=run_id, dedupe_key=f"execute:{run_id}"
                )
            )
            return CreateRunResult(run=self._load_runs(conn, [run_id])[0], replayed=False)

    def get_run_create(self, run_id: UUID) -> RunCreate | None:
        """The request that would reproduce this run's inputs (for a fresh rerun)."""
        with self._engine.connect() as conn:
            row = conn.execute(
                select(t.runs.c.manifest, t.runs.c.labels).where(t.runs.c.id == run_id)
            ).first()
        if row is None:
            return None
        m = Manifest.model_validate(row.manifest)
        return RunCreate(
            schema_version="1.0",
            scenario=ScenarioRef(id=m.scenario.id, version=m.scenario.version),
            agent=AgentRef(id=m.agent.id, version=m.agent.version, config=m.agent.config),
            seed=m.seed,
            mode=m.mode,
            budgets=m.budgets,
            labels=row.labels,
        )

    def get_run(self, run_id: UUID) -> RunRecord | None:
        with self._engine.connect() as conn:
            found = self._load_runs(conn, [run_id])
        return found[0] if found else None

    def list_runs(
        self, status: str | None, scenario_id: str | None, cursor: str | None, limit: int
    ) -> RunPage:
        r = t.runs
        query = select(r.c.id, r.c.created_at).order_by(r.c.created_at.desc(), r.c.id.desc())
        if status is not None:
            query = query.where(r.c.status == status)
        if scenario_id is not None:
            query = query.where(r.c.scenario_id == scenario_id)
        if cursor is not None:
            created_at, key = decode_cursor(cursor, 1)
            try:
                after = UUID(key[0])
            except ValueError:
                raise InvalidCursor from None
            query = query.where(tuple_(r.c.created_at, r.c.id) < tuple_(created_at, after))
        with self._engine.connect() as conn:
            page = conn.execute(query.limit(limit + 1)).all()
            ids = [row.id for row in page[:limit]]
            items = self._load_runs(conn, ids)
        more = len(page) > limit
        next_cursor = None
        if more:
            tail = page[limit - 1]
            next_cursor = encode_cursor(tail.created_at, [str(tail.id)])
        return RunPage(items=items, next_cursor=next_cursor)

    # --- cancellation, events, evaluations --------------------------------------------------

    def cancel_run(self, run_id: UUID, reason: str | None) -> CancelResult | None:
        """Idempotent. A run with no open attempt (queued, or waiting for a replacement
        attempt) finishes at once with no evaluation; otherwise the durable flag is set and the
        supervisor closes the attempt. Lock order job -> run, like claim and reaper."""
        r, j, a = t.runs, t.jobs, t.run_attempts
        with self._engine.begin() as conn:
            job = conn.execute(
                select(j.c.id, j.c.status)
                .where(
                    j.c.run_id == run_id,
                    j.c.kind == "execute",
                    j.c.status.in_(("queued", "leased")),
                )
                .with_for_update()
            ).first()
            run = conn.execute(select(r.c.status).where(r.c.id == run_id).with_for_update()).first()
            if run is None:
                return None
            if run.status in ("completed", "error"):
                return CancelResult(run=self._load_runs(conn, [run_id])[0], accepted=False)
            conn.execute(update(r).where(r.c.id == run_id).values(cancel_requested=True))
            open_attempts = conn.execute(
                select(func.count()).where(a.c.run_id == run_id, a.c.closed_at.is_(None))
            ).scalar_one()
            if job is not None and job.status == "queued" and open_attempts == 0:
                conn.execute(update(j).where(j.c.id == job.id).values(status="done"))
                conn.execute(
                    update(r)
                    .where(r.c.id == run_id)
                    .values(status="completed", closed_at=func.now())
                )
            return CancelResult(run=self._load_runs(conn, [run_id])[0], accepted=True)

    def list_events(
        self, run_id: UUID, attempt_id: UUID | None, after_seq: int, limit: int
    ) -> EventPage | None:
        a, ev = t.run_attempts, t.events
        with self._engine.connect() as conn:
            if conn.execute(select(t.runs.c.id).where(t.runs.c.id == run_id)).first() is None:
                return None
            if attempt_id is None:
                attempt_id = conn.execute(
                    select(a.c.id).where(a.c.run_id == run_id).order_by(a.c.ordinal.desc()).limit(1)
                ).scalar()
                if attempt_id is None:
                    return EventPage(
                        items=[], next_after_seq=after_seq, has_more=False, attempt_closed=False
                    )
            else:
                owner = conn.execute(select(a.c.run_id).where(a.c.id == attempt_id)).scalar()
                if owner != run_id:
                    raise AttemptNotInRun
            rows = conn.execute(
                select(ev)
                .where(ev.c.attempt_id == attempt_id, ev.c.seq > after_seq)
                .order_by(ev.c.seq)
                .limit(limit + 1)
            ).all()
            closed = conn.execute(
                select(a.c.closed_at.is_not(None)).where(a.c.id == attempt_id)
            ).scalar_one()
        page = rows[:limit]
        items = [
            EventRecord(
                run_id=run_id,
                attempt_id=attempt_id,
                seq=e.seq,
                type=e.type,
                wall_time=e.wall_time,
                logical_time_ms=e.logical_time_ms,
                actor_id=e.actor_id,
                call_id=e.call_id,
                payload=e.payload,
                prev_hash=e.prev_hash,
                event_hash=e.event_hash,
            )
            for e in page
        ]
        return EventPage(
            items=items,
            next_after_seq=items[-1].seq if items else after_seq,
            has_more=len(rows) > limit,
            attempt_closed=bool(closed),
        )

    def list_evaluations(
        self, run_id: UUID, attempt_id: UUID | None
    ) -> list[EvaluationRecord] | None:
        a, e = t.run_attempts, t.evaluations
        with self._engine.connect() as conn:
            if conn.execute(select(t.runs.c.id).where(t.runs.c.id == run_id)).first() is None:
                return None
            query = select(e).join(a, a.c.id == e.c.attempt_id).where(a.c.run_id == run_id)
            if attempt_id is not None:
                query = query.where(e.c.attempt_id == attempt_id)
            rows = conn.execute(query.order_by(a.c.ordinal, e.c.evaluated_at)).all()
        return [
            EvaluationRecord(
                id=row.id,
                attempt_id=row.attempt_id,
                grader_set_hash=row.grader_set_hash,
                snapshot_hash=row.snapshot_hash,
                verdict=row.verdict,
                grades=[Grade.model_validate(g) for g in row.grades],
                primary_reason=row.primary_reason,
                findings=[Finding.model_validate(f) for f in row.findings],
                evaluated_at=row.evaluated_at,
            )
            for row in rows
        ]

    @staticmethod
    def _load_runs(conn: Connection, ids: list[UUID]) -> list[RunRecord]:
        """Runs in the order of ``ids`` (missing ids are skipped), with their attempts."""
        if not ids:
            return []
        rows = {row.id: row for row in conn.execute(select(t.runs).where(t.runs.c.id.in_(ids)))}
        attempts: dict[UUID, list[AttemptSummary]] = {}
        for a in conn.execute(
            select(t.run_attempts)
            .where(t.run_attempts.c.run_id.in_(ids))
            .order_by(t.run_attempts.c.ordinal)
        ):
            attempts.setdefault(a.run_id, []).append(
                AttemptSummary(id=a.id, ordinal=a.ordinal, termination=a.termination)
            )
        out = []
        for run_id in ids:
            row = rows.get(run_id)
            if row is None:
                continue
            created_at: datetime = row.created_at
            out.append(
                RunRecord(
                    id=row.id,
                    status=row.status,
                    verdict=row.verdict,
                    manifest_hash=row.manifest_hash,
                    selected_attempt_id=row.selected_attempt_id,
                    attempts=attempts.get(row.id, []),
                    parent_run_id=row.parent_run_id,
                    replay_capability=row.manifest["replay_capability"],
                    scenario=ScenarioRef(id=row.scenario_id, version=row.scenario_version),
                    agent=AgentIdentity(id=row.agent_id, version=row.agent_version),
                    seed=str(int(row.seed)),
                    labels=row.labels,
                    created_at=created_at,
                    completed_at=row.closed_at,
                    error_code=row.error_code,
                )
            )
        return out
