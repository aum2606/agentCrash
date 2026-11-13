"""PostgreSQL job queue: claim, lease, heartbeat, reaper and the attempt fence.

ENGINEERING_PLAN.md section 3. Every method is one short transaction; the claim never holds a
transaction open while a run executes. PostgreSQL ``now()`` is the only clock for lease expiry.
Lock order is always job row, then run row, so claim, reaper and fenced writes cannot deadlock.
"""

from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import Connection, Engine, func, select, update

from agentcrash.domain.ports import (
    WORKER_TERMINATIONS,
    Claim,
    GradeClaim,
    Reaped,
    StaleFence,
    SubjectClaim,
)
from agentcrash.storage import schema as t

LEASE_SECONDS = 30
MAX_EXECUTION_ATTEMPTS = 2
_REAP_BATCH = 100
MAX_GRADE_DELIVERIES = 3


def lock_fenced_attempt(conn: Connection, attempt_id: UUID, fencing_token: int) -> UUID:
    """Lock the attempt's run row and prove ``fencing_token`` is still the active one.

    Returns the run id. Raises ``StaleFence`` if the run was re-fenced (reaper reassigned it),
    the attempt belongs to an older token, or the attempt is already closed. Event appends,
    environment writes and completion must call this inside their own transaction so the check
    and the write are atomic.
    """
    run_id = conn.execute(
        select(t.run_attempts.c.run_id).where(t.run_attempts.c.id == attempt_id)
    ).scalar()
    if run_id is None:
        raise StaleFence
    # Lock order job -> run -> attempt: the run lock is taken first. Every attempt change also
    # happens under the run lock, so the attempt row read below cannot change under us.
    run_token = conn.execute(
        select(t.runs.c.fencing_token).where(t.runs.c.id == run_id).with_for_update()
    ).scalar_one()
    attempt = conn.execute(
        select(t.run_attempts.c.fencing_token, t.run_attempts.c.closed_at).where(
            t.run_attempts.c.id == attempt_id
        )
    ).one()
    if (
        attempt.closed_at is not None
        or attempt.fencing_token != fencing_token
        or run_token != fencing_token
    ):
        raise StaleFence
    return run_id  # type: ignore[no-any-return]


class JobQueue:
    def __init__(
        self,
        engine: Engine,
        *,
        lease_seconds: int = LEASE_SECONDS,
        max_attempts: int = MAX_EXECUTION_ATTEMPTS,
    ) -> None:
        self._engine = engine
        self._lease = lease_seconds
        self._max_attempts = max_attempts

    def claim_execute(self, owner: str) -> Claim | None:
        """Lease the oldest available execute job and create a fresh attempt for it.

        ``FOR UPDATE SKIP LOCKED`` lets concurrent workers pass over each other's rows. The run's
        fencing token is incremented, so any earlier holder of this run is fenced out.
        """
        j, r = t.jobs, t.runs
        with self._engine.begin() as conn:
            job_id = conn.execute(
                select(j.c.id)
                .join(r, r.c.id == j.c.run_id)
                .where(
                    j.c.kind == "execute",
                    j.c.status == "queued",
                    j.c.available_at <= func.now(),
                    r.c.status.in_(("queued", "running")),
                    r.c.cancel_requested.is_(False),
                )
                .order_by(j.c.available_at, j.c.created_at, j.c.id)
                .limit(1)
                .with_for_update(skip_locked=True, of=j)
            ).scalar()
            if job_id is None:
                return None
            run_id = conn.execute(select(j.c.run_id).where(j.c.id == job_id)).scalar_one()
            token = conn.execute(
                update(r)
                .where(r.c.id == run_id)
                .values(status="running", fencing_token=r.c.fencing_token + 1)
                .returning(r.c.fencing_token)
            ).scalar_one()
            ordinal = (
                conn.execute(
                    select(func.coalesce(func.max(t.run_attempts.c.ordinal), 0)).where(
                        t.run_attempts.c.run_id == run_id
                    )
                ).scalar_one()
                + 1
            )
            attempt_id = uuid4()
            conn.execute(
                t.run_attempts.insert().values(
                    id=attempt_id, run_id=run_id, ordinal=ordinal, fencing_token=token
                )
            )
            lease_until = conn.execute(
                update(j)
                .where(j.c.id == job_id)
                .values(
                    status="leased",
                    owner=owner,
                    fencing_token=token,
                    attempt_id=attempt_id,
                    delivery_count=j.c.delivery_count + 1,
                    lease_until=func.now() + func.make_interval(0, 0, 0, 0, 0, 0, self._lease),
                )
                .returning(j.c.lease_until)
            ).scalar_one()
        return Claim(
            job_id=job_id,
            run_id=run_id,
            attempt_id=attempt_id,
            ordinal=ordinal,
            fencing_token=token,
            owner=owner,
            lease_until=lease_until,
        )

    def heartbeat(self, job_id: UUID, owner: str, fencing_token: int) -> bool:
        """Extend a live lease. False means the lease was lost (expired, reaped or re-fenced):
        the worker must stop and must not publish anything further."""
        j = t.jobs
        with self._engine.begin() as conn:
            return (
                conn.execute(
                    update(j)
                    .where(
                        j.c.id == job_id,
                        j.c.status == "leased",
                        j.c.owner == owner,
                        j.c.fencing_token == fencing_token,
                        j.c.lease_until > func.now(),
                    )
                    .values(
                        lease_until=func.now() + func.make_interval(0, 0, 0, 0, 0, 0, self._lease)
                    )
                    .returning(j.c.id)
                ).first()
                is not None
            )

    def close_attempt(self, attempt_id: UUID, fencing_token: int, termination: str) -> None:
        """Close an attempt once, under the fence. Raises ``StaleFence`` for a stale holder."""
        if termination not in WORKER_TERMINATIONS:
            raise ValueError(f"termination {termination!r} cannot be reported by a worker")
        with self._engine.begin() as conn:
            lock_fenced_attempt(conn, attempt_id, fencing_token)
            conn.execute(
                update(t.run_attempts)
                .where(t.run_attempts.c.id == attempt_id)
                .values(termination=termination, closed_at=func.now())
            )

    def _lose(self, conn: Connection, job: Any) -> Reaped | None:
        """Close ``job``'s open attempt as ``worker_lost`` and fence its token out; then queue a
        fresh attempt, finish a cancelled run, or (at the attempt cap) fail the run. Runs inside
        the caller's transaction, which already holds the job row lock."""
        j, r, a = t.jobs, t.runs, t.run_attempts
        # Run lock first, then re-check the attempt: a worker that closed it just before us
        # must not be overwritten with worker_lost.
        cancelled = conn.execute(
            select(r.c.cancel_requested).where(r.c.id == job.run_id).with_for_update()
        ).scalar_one()
        ordinal = conn.execute(
            update(a)
            .where(a.c.id == job.attempt_id, a.c.closed_at.is_(None))
            .values(termination="worker_lost", closed_at=func.now())
            .returning(a.c.ordinal)
        ).scalar()
        if ordinal is None:
            return None
        conn.execute(  # invalidates the lost worker's token
            update(r).where(r.c.id == job.run_id).values(fencing_token=r.c.fencing_token + 1)
        )
        conn.execute(update(j).where(j.c.id == job.id).values(status="error"))
        requeue = not cancelled and ordinal < self._max_attempts
        if requeue:
            conn.execute(
                j.insert().values(
                    id=uuid4(),
                    kind="execute",
                    run_id=job.run_id,
                    dedupe_key=f"execute:{job.run_id}:{ordinal + 1}",
                )
            )
        elif cancelled:  # cancellation wins over recovery: no replacement attempt
            conn.execute(
                update(r)
                .where(r.c.id == job.run_id)
                .values(status="completed", closed_at=func.now())
            )
        else:
            conn.execute(
                update(r)
                .where(r.c.id == job.run_id)
                .values(status="error", error_code="INFRA_WORKER_LOST", closed_at=func.now())
            )
        return Reaped(job.run_id, job.attempt_id, ordinal, requeue)

    def abandon(self, job_id: UUID, owner: str, fencing_token: int) -> Reaped | None:
        """The supervisor's own recovery path (child died, shutdown drain expired): same
        transition as the reaper, without waiting for the lease. ``None`` if already handled."""
        j = t.jobs
        with self._engine.begin() as conn:
            job = conn.execute(
                select(j.c.id, j.c.run_id, j.c.attempt_id)
                .where(
                    j.c.id == job_id,
                    j.c.kind == "execute",
                    j.c.status == "leased",
                    j.c.owner == owner,
                    j.c.fencing_token == fencing_token,
                )
                .with_for_update()
            ).first()
            return None if job is None else self._lose(conn, job)

    def reap_expired(self) -> list[Reaped]:
        """Recover jobs whose lease expired. An execute attempt is closed ``worker_lost`` and
        replaced (or the run fails at the cap); an expired grading lease simply re-queues the
        job, which is safe because publishing an evaluation is idempotent."""
        j, a = t.jobs, t.run_attempts
        reaped: list[Reaped] = []
        with self._engine.begin() as conn:
            conn.execute(
                update(j)
                .where(
                    j.c.kind.in_(("grade", "export", "replay")),
                    j.c.status == "leased",
                    j.c.lease_until < func.now(),
                )
                .values(status="queued", owner=None, lease_until=None)
            )
            expired = conn.execute(
                select(j.c.id, j.c.run_id, j.c.attempt_id)
                .join(a, a.c.id == j.c.attempt_id)
                .where(
                    j.c.kind == "execute",
                    j.c.status == "leased",
                    j.c.lease_until < func.now(),
                    a.c.closed_at.is_(None),
                )
                .order_by(j.c.lease_until, j.c.id)
                .limit(_REAP_BATCH)
                .with_for_update(skip_locked=True, of=j)
            ).all()
            for job in expired:
                lost = self._lose(conn, job)
                if lost is not None:
                    reaped.append(lost)
        return reaped

    def claim_grade(self, owner: str) -> GradeClaim | None:
        """Lease the oldest grading job. No attempt or fencing bump: grading reads an immutable,
        closed attempt and publishes idempotently."""
        j = t.jobs
        with self._engine.begin() as conn:
            job = conn.execute(
                select(j.c.id, j.c.run_id, j.c.attempt_id, j.c.delivery_count)
                .where(j.c.kind == "grade", j.c.status == "queued", j.c.available_at <= func.now())
                .order_by(j.c.available_at, j.c.created_at, j.c.id)
                .limit(1)
                .with_for_update(skip_locked=True)
            ).first()
            if job is None:
                return None
            if job.delivery_count >= MAX_GRADE_DELIVERIES:  # poison job: fail the run visibly
                conn.execute(update(j).where(j.c.id == job.id).values(status="error"))
                conn.execute(
                    update(t.runs)
                    .where(t.runs.c.id == job.run_id, t.runs.c.status == "evaluating")
                    .values(status="error", error_code="EVALUATOR_ERROR", closed_at=func.now())
                )
                return None
            conn.execute(
                update(j)
                .where(j.c.id == job.id)
                .values(
                    status="leased",
                    owner=owner,
                    delivery_count=j.c.delivery_count + 1,
                    lease_until=func.now() + func.make_interval(0, 0, 0, 0, 0, 0, self._lease),
                )
            )
        return GradeClaim(job.id, job.run_id, job.attempt_id)

    def claim_subject(self, kind: str, owner: str) -> SubjectClaim | None:
        """Lease the oldest export or replay job and mark its row ``running``. Both read an
        immutable, closed attempt and never touch run state, so a redelivered job is harmless:
        a job delivered ``MAX_GRADE_DELIVERIES`` times is failed visibly instead of retried."""
        j = t.jobs
        table = {"export": t.exports, "replay": t.replays}[kind]
        with self._engine.begin() as conn:
            job = conn.execute(
                select(j.c.id, j.c.run_id, j.c.attempt_id, j.c.subject_id, j.c.delivery_count)
                .where(j.c.kind == kind, j.c.status == "queued", j.c.available_at <= func.now())
                .order_by(j.c.available_at, j.c.created_at, j.c.id)
                .limit(1)
                .with_for_update(skip_locked=True)
            ).first()
            if job is None:
                return None
            if job.delivery_count >= MAX_GRADE_DELIVERIES:
                conn.execute(update(j).where(j.c.id == job.id).values(status="error"))
                conn.execute(
                    update(table)
                    .where(table.c.id == job.subject_id, table.c.status != "completed")
                    .values(status="error", error_code="JOB_FAILED", closed_at=func.now())
                )
                return None
            conn.execute(
                update(j)
                .where(j.c.id == job.id)
                .values(
                    status="leased",
                    owner=owner,
                    delivery_count=j.c.delivery_count + 1,
                    lease_until=func.now() + func.make_interval(0, 0, 0, 0, 0, 0, self._lease),
                )
            )
            conn.execute(
                update(table)
                .where(table.c.id == job.subject_id, table.c.status == "queued")
                .values(status="running")
            )
        return SubjectClaim(job.id, job.run_id, job.attempt_id, job.subject_id)
