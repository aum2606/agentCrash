"""SQLAlchemy Core table definitions used by the repositories (CONTRACTS.md section 8).

The authoritative DDL is the Alembic migration; ``tests/integration`` compares the migrated
database with this metadata so the two cannot silently diverge.
"""

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    Column,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    MetaData,
    Numeric,
    PrimaryKeyConstraint,
    Table,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, TIMESTAMP, UUID

metadata = MetaData()

RUN_STATUSES = ("queued", "running", "evaluating", "completed", "error")
VERDICTS = ("pass", "fail", "inconclusive")
JOB_KINDS = ("execute", "grade", "replay", "export")
JOB_STATUSES = ("queued", "leased", "done", "error")


def _in(column: str, values: tuple[str, ...]) -> str:
    return f"{column} IN ({', '.join(repr(v) for v in values)})"


def _created() -> Column[object]:
    return Column("created_at", TIMESTAMP(timezone=True), nullable=False, server_default=func.now())


scenario_versions = Table(
    "scenario_versions",
    metadata,
    Column("id", Text, nullable=False),
    Column("version", Integer, nullable=False),
    Column("content_hash", Text, nullable=False),
    Column("schema_version", Text, nullable=False),
    Column("normalized_json", JSONB, nullable=False),
    _created(),
    PrimaryKeyConstraint("id", "version"),
    UniqueConstraint("content_hash"),
)

agent_versions = Table(
    "agent_versions",
    metadata,
    Column("id", Text, nullable=False),
    Column("version", Text, nullable=False),
    Column("adapter_registry_key", Text, nullable=False),
    Column("config_schema_hash", Text, nullable=False),
    Column("source_hash", Text),
    _created(),
    PrimaryKeyConstraint("id", "version"),
)

experiments = Table(
    "experiments",
    metadata,
    Column("id", UUID(as_uuid=True), primary_key=True),
    Column("config_json", JSONB, nullable=False),
    Column("config_hash", Text, nullable=False),
    Column("status", Text, nullable=False),
    _created(),
)

runs = Table(
    "runs",
    metadata,
    Column("id", UUID(as_uuid=True), primary_key=True),
    Column("experiment_id", UUID(as_uuid=True), ForeignKey("experiments.id")),
    Column("parent_run_id", UUID(as_uuid=True), ForeignKey("runs.id")),
    Column("scenario_id", Text, nullable=False),
    Column("scenario_version", Integer, nullable=False),
    Column("agent_id", Text, nullable=False),
    Column("agent_version", Text, nullable=False),
    Column("seed", Numeric(20, 0), nullable=False),
    Column("manifest", JSONB, nullable=False),
    Column("manifest_hash", Text, nullable=False),
    Column("labels", JSONB, nullable=False, server_default=text("'{}'::jsonb")),
    Column("status", Text, nullable=False, server_default="queued"),
    Column("verdict", Text),
    Column("cancel_requested", Boolean, nullable=False, server_default=text("false")),
    Column("fencing_token", BigInteger, nullable=False, server_default=text("0")),
    Column(
        "selected_attempt_id",
        UUID(as_uuid=True),
        # runs and run_attempts reference each other, so this FK is added after both exist.
        ForeignKey("run_attempts.id", use_alter=True, name="runs_selected_attempt_fk"),
    ),
    _created(),
    Column("closed_at", TIMESTAMP(timezone=True)),
    ForeignKeyConstraint(
        ["scenario_id", "scenario_version"],
        ["scenario_versions.id", "scenario_versions.version"],
    ),
    ForeignKeyConstraint(
        ["agent_id", "agent_version"], ["agent_versions.id", "agent_versions.version"]
    ),
    CheckConstraint(_in("status", RUN_STATUSES), name="runs_status_check"),
    CheckConstraint(f"verdict IS NULL OR {_in('verdict', VERDICTS)}", name="runs_verdict_check"),
    CheckConstraint("seed >= 0 AND seed < 18446744073709551616", name="runs_seed_u64_check"),
)
Index("runs_created_idx", runs.c.created_at, runs.c.id)
Index("runs_status_created_idx", runs.c.status, runs.c.created_at, runs.c.id)
Index("runs_experiment_idx", runs.c.experiment_id)

run_attempts = Table(
    "run_attempts",
    metadata,
    Column("id", UUID(as_uuid=True), primary_key=True),
    Column("run_id", UUID(as_uuid=True), ForeignKey("runs.id"), nullable=False),
    Column("ordinal", Integer, nullable=False),
    Column("fencing_token", BigInteger, nullable=False),
    Column("termination", Text),
    Column("initial_state", JSONB),
    Column("final_state", JSONB),
    Column("final_state_hash", Text),
    Column("next_seq", BigInteger, nullable=False, server_default=text("1")),
    Column("chain_head", Text),
    _created(),
    Column("closed_at", TIMESTAMP(timezone=True)),
    UniqueConstraint("run_id", "ordinal"),
)

jobs = Table(
    "jobs",
    metadata,
    Column("id", UUID(as_uuid=True), primary_key=True),
    Column("kind", Text, nullable=False),
    Column("run_id", UUID(as_uuid=True), ForeignKey("runs.id"), nullable=False),
    Column("attempt_id", UUID(as_uuid=True), ForeignKey("run_attempts.id")),
    Column("dedupe_key", Text, nullable=False),
    Column("status", Text, nullable=False, server_default="queued"),
    Column("available_at", TIMESTAMP(timezone=True), nullable=False, server_default=func.now()),
    Column("lease_until", TIMESTAMP(timezone=True)),
    Column("owner", Text),
    Column("fencing_token", BigInteger, nullable=False, server_default=text("0")),
    Column("delivery_count", Integer, nullable=False, server_default=text("0")),
    _created(),
    UniqueConstraint("dedupe_key"),
    CheckConstraint(_in("kind", JOB_KINDS), name="jobs_kind_check"),
    CheckConstraint(_in("status", JOB_STATUSES), name="jobs_status_check"),
)
Index(
    "jobs_one_active_execute_per_run",
    jobs.c.run_id,
    unique=True,
    postgresql_where=text("kind = 'execute' AND status IN ('queued', 'leased')"),
)
Index(
    "jobs_claim_idx",
    jobs.c.status,
    jobs.c.available_at,
    jobs.c.lease_until,
    postgresql_where=text("status IN ('queued', 'leased')"),
)

api_idempotency = Table(
    "api_idempotency",
    metadata,
    Column("principal", Text, nullable=False),
    Column("route", Text, nullable=False),
    Column("key", Text, nullable=False),
    Column("request_hash", Text, nullable=False),
    Column("resource_type", Text, nullable=False),
    Column("resource_id", UUID(as_uuid=True), nullable=False),
    _created(),
    Column("tombstoned_at", TIMESTAMP(timezone=True)),
    PrimaryKeyConstraint("principal", "route", "key"),
)
