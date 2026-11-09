"""Execution evidence: events, tool calls, environment state, receipts, ledger, evaluations.

Revision ID: 0002
Revises: 0001
Additive (expand-only): new tables, one nullable column, one CHECK on existing data that is
always NULL at this revision. Events and receipts are append-only (enforced by trigger).
"""

from alembic import op

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None

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
TERMINATIONS = (
    "agent_completed",
    "agent_error",
    "budget_exceeded",
    "deadline_exceeded",
    "cancelled",
    "worker_lost",
)


def _in(values: tuple[str, ...]) -> str:
    return ", ".join(repr(v) for v in values)


STATEMENTS = [
    "ALTER TABLE runs ADD COLUMN error_code text",
    f"""
    ALTER TABLE run_attempts ADD CONSTRAINT run_attempts_termination_check
        CHECK (termination IS NULL OR termination IN ({_in(TERMINATIONS)}))
    """,
    f"""
    CREATE TABLE events (
        attempt_id uuid NOT NULL REFERENCES run_attempts (id),
        seq bigint NOT NULL,
        run_id uuid NOT NULL REFERENCES runs (id),
        type text NOT NULL,
        logical_time_ms bigint NOT NULL,
        wall_time timestamptz NOT NULL,
        actor_id text,
        call_id text,
        payload jsonb NOT NULL,
        prev_hash text NOT NULL,
        event_hash text NOT NULL,
        PRIMARY KEY (attempt_id, seq),
        CONSTRAINT events_type_check CHECK (type IN ({_in(EVENT_TYPES)}))
    )
    """,
    "CREATE INDEX events_run_idx ON events (run_id)",
    """
    CREATE TRIGGER events_append_only BEFORE UPDATE OR DELETE ON events
        FOR EACH ROW EXECUTE FUNCTION agentcrash_reject_change()
    """,
    """
    CREATE TABLE tool_calls (
        attempt_id uuid NOT NULL REFERENCES run_attempts (id),
        call_id text NOT NULL,
        call_index integer NOT NULL,
        tool_ordinal integer NOT NULL,
        actor_id text NOT NULL,
        tool text NOT NULL,
        input_hash text NOT NULL,
        outcome jsonb NOT NULL,
        terminal boolean NOT NULL DEFAULT true,
        PRIMARY KEY (attempt_id, call_id),
        UNIQUE (attempt_id, call_index)
    )
    """,
    """
    CREATE TABLE environment_rows (
        attempt_id uuid NOT NULL REFERENCES run_attempts (id),
        entity_type text NOT NULL,
        entity_id text NOT NULL,
        version integer NOT NULL,
        data jsonb NOT NULL,
        PRIMARY KEY (attempt_id, entity_type, entity_id)
    )
    """,
    """
    CREATE TABLE command_receipts (
        attempt_id uuid NOT NULL REFERENCES run_attempts (id),
        idempotency_key text NOT NULL,
        command_hash text NOT NULL,
        semantic_receipt_id text NOT NULL,
        result jsonb NOT NULL,
        PRIMARY KEY (attempt_id, idempotency_key)
    )
    """,
    """
    CREATE TRIGGER command_receipts_immutable BEFORE UPDATE OR DELETE ON command_receipts
        FOR EACH ROW EXECUTE FUNCTION agentcrash_reject_change()
    """,
    """
    CREATE TABLE ledger_entries (
        attempt_id uuid NOT NULL REFERENCES run_attempts (id),
        semantic_entry_id text NOT NULL,
        invoice_id text NOT NULL,
        data jsonb NOT NULL,
        PRIMARY KEY (attempt_id, semantic_entry_id),
        UNIQUE (attempt_id, invoice_id)
    )
    """,
    """
    CREATE TABLE evaluations (
        id uuid PRIMARY KEY,
        attempt_id uuid NOT NULL REFERENCES run_attempts (id),
        grader_set_hash text NOT NULL,
        snapshot_hash text,
        verdict text NOT NULL,
        grades jsonb NOT NULL,
        evaluated_at timestamptz NOT NULL DEFAULT now(),
        UNIQUE (attempt_id, grader_set_hash),
        CONSTRAINT evaluations_verdict_check CHECK (verdict IN ('pass', 'fail', 'inconclusive'))
    )
    """,
]


def upgrade() -> None:
    for statement in STATEMENTS:
        op.execute(statement)


def downgrade() -> None:
    for statement in (
        "DROP TABLE evaluations",
        "DROP TABLE ledger_entries",
        "DROP TABLE command_receipts",
        "DROP TABLE environment_rows",
        "DROP TABLE tool_calls",
        "DROP TABLE events",
        "ALTER TABLE run_attempts DROP CONSTRAINT run_attempts_termination_check",
        "ALTER TABLE runs DROP COLUMN error_code",
    ):
        op.execute(statement)
