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
EVENT_TYPES = (
    "attempt.started",
    "agent.step",
    "tool.requested",
    "policy.decided",
    "fault.decided",
    "environment.mutated",
    "tool.completed",
    "tool.transport_failed",
    "clock.advanced",
    "agent.finished",
    "attempt.closed",
)
JOB_KINDS = ("execute", "grade", "replay", "export")
JOB_STATUSES = ("queued", "leased", "done", "error")
TERMINATIONS = (
    "agent_completed",
    "agent_error",
    "budget_exceeded",
    "deadline_exceeded",
    "cancelled",
    "worker_lost",
)


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
    Column("error_code", Text),
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
    CheckConstraint(
        f"termination IS NULL OR {_in('termination', TERMINATIONS)}",
        name="run_attempts_termination_check",
    ),
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

events = Table(
    "events",
    metadata,
    Column("attempt_id", UUID(as_uuid=True), ForeignKey("run_attempts.id"), nullable=False),
    Column("seq", BigInteger, nullable=False),
    Column("run_id", UUID(as_uuid=True), ForeignKey("runs.id"), nullable=False),
    Column("type", Text, nullable=False),
    Column("logical_time_ms", BigInteger, nullable=False),
    Column("wall_time", TIMESTAMP(timezone=True), nullable=False),
    Column("actor_id", Text),
    Column("call_id", Text),
    Column("payload", JSONB, nullable=False),
    Column("prev_hash", Text, nullable=False),
    Column("event_hash", Text, nullable=False),
    PrimaryKeyConstraint("attempt_id", "seq"),
    CheckConstraint(_in("type", EVENT_TYPES), name="events_type_check"),
)
Index("events_run_idx", events.c.run_id)

tool_calls = Table(
    "tool_calls",
    metadata,
    Column("attempt_id", UUID(as_uuid=True), ForeignKey("run_attempts.id"), nullable=False),
    Column("call_id", Text, nullable=False),
    Column("call_index", Integer, nullable=False),
    Column("tool_ordinal", Integer, nullable=False),
    Column("actor_id", Text, nullable=False),
    Column("tool", Text, nullable=False),
    Column("input_hash", Text, nullable=False),
    Column("outcome", JSONB, nullable=False),
    Column("terminal", Boolean, nullable=False, server_default=text("true")),
    PrimaryKeyConstraint("attempt_id", "call_id"),
    UniqueConstraint("attempt_id", "call_index"),
)

environment_rows = Table(
    "environment_rows",
    metadata,
    Column("attempt_id", UUID(as_uuid=True), ForeignKey("run_attempts.id"), nullable=False),
    Column("entity_type", Text, nullable=False),
    Column("entity_id", Text, nullable=False),
    Column("version", Integer, nullable=False),
    Column("data", JSONB, nullable=False),
    PrimaryKeyConstraint("attempt_id", "entity_type", "entity_id"),
)

command_receipts = Table(
    "command_receipts",
    metadata,
    Column("attempt_id", UUID(as_uuid=True), ForeignKey("run_attempts.id"), nullable=False),
    Column("idempotency_key", Text, nullable=False),
    Column("command_hash", Text, nullable=False),
    Column("semantic_receipt_id", Text, nullable=False),
    Column("result", JSONB, nullable=False),
    PrimaryKeyConstraint("attempt_id", "idempotency_key"),
)

ledger_entries = Table(
    "ledger_entries",
    metadata,
    Column("attempt_id", UUID(as_uuid=True), ForeignKey("run_attempts.id"), nullable=False),
    Column("semantic_entry_id", Text, nullable=False),
    Column("invoice_id", Text, nullable=False),
    Column("data", JSONB, nullable=False),
    PrimaryKeyConstraint("attempt_id", "semantic_entry_id"),
    UniqueConstraint("attempt_id", "invoice_id"),
)

evaluations = Table(
    "evaluations",
    metadata,
    Column("id", UUID(as_uuid=True), primary_key=True),
    Column("attempt_id", UUID(as_uuid=True), ForeignKey("run_attempts.id"), nullable=False),
    Column("grader_set_hash", Text, nullable=False),
    Column("snapshot_hash", Text),
    Column("verdict", Text, nullable=False),
    Column("grades", JSONB, nullable=False),
    Column("primary_reason", Text),
    Column("findings", JSONB, nullable=False, server_default=text("'[]'::jsonb")),
    Column("evaluated_at", TIMESTAMP(timezone=True), nullable=False, server_default=func.now()),
    UniqueConstraint("attempt_id", "grader_set_hash"),
    CheckConstraint(_in("verdict", VERDICTS), name="evaluations_verdict_check"),
)
