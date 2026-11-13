"""Run admission shared by single-run and experiment creation: one lock serializes the queue-cap
check with the inserts, and a run is always written together with its execute job."""

from uuid import UUID, uuid4

from sqlalchemy import Connection, func, select, text

from agentcrash.domain.models import Manifest, RunCreate
from agentcrash.domain.ports import QueueFull
from agentcrash.storage import schema as t

# Serializes the "count queued, then insert" admission check across transactions.
ADMISSION_LOCK_KEY = 0x4143_4C5F_4144_4D31  # "ACL_ADM1"


def admit(conn: Connection, count: int, queue_cap: int) -> None:
    """Raise ``QueueFull`` unless ``count`` more queued runs fit under the cap. The lock is held
    until the caller's transaction ends, so the cap cannot be overshot by a concurrent request."""
    conn.execute(text("SELECT pg_advisory_xact_lock(:k)"), {"k": ADMISSION_LOCK_KEY})
    queued = conn.execute(
        select(func.count()).select_from(t.runs).where(t.runs.c.status == "queued")
    ).scalar_one()
    if queued + count > queue_cap:
        raise QueueFull


def insert_run(
    conn: Connection,
    run_id: UUID,
    run: RunCreate,
    manifest: Manifest,
    manifest_hash: str,
    parent_run_id: UUID | None = None,
    experiment_id: UUID | None = None,
) -> None:
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
            experiment_id=experiment_id,
        )
    )
    conn.execute(
        t.jobs.insert().values(
            id=uuid4(), kind="execute", run_id=run_id, dedupe_key=f"execute:{run_id}"
        )
    )
