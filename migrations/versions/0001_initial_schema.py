"""Initial schema: scenarios, agents, experiments, runs, attempts, jobs, API idempotency.

Revision ID: 0001
Revises:
Frozen SQL: do not edit after release; add a new revision instead (expand/migrate/contract).
"""

from alembic import op

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None

STATEMENTS = [
    """
    CREATE TABLE scenario_versions (
        id text NOT NULL,
        version integer NOT NULL,
        content_hash text NOT NULL UNIQUE,
        schema_version text NOT NULL,
        normalized_json jsonb NOT NULL,
        created_at timestamptz NOT NULL DEFAULT now(),
        PRIMARY KEY (id, version)
    )
    """,
    # Scenario versions are immutable (CONTRACTS section 8): enforced by the database, not
    # only by application code.
    """
    CREATE FUNCTION agentcrash_reject_change() RETURNS trigger LANGUAGE plpgsql AS $$
    BEGIN
        RAISE EXCEPTION '% is immutable', TG_TABLE_NAME
            USING ERRCODE = 'integrity_constraint_violation';
    END $$
    """,
    """
    CREATE TRIGGER scenario_versions_immutable
        BEFORE UPDATE OR DELETE ON scenario_versions
        FOR EACH ROW EXECUTE FUNCTION agentcrash_reject_change()
    """,
    """
    CREATE TABLE agent_versions (
        id text NOT NULL,
        version text NOT NULL,
        adapter_registry_key text NOT NULL,
        config_schema_hash text NOT NULL,
        source_hash text,
        created_at timestamptz NOT NULL DEFAULT now(),
        PRIMARY KEY (id, version)
    )
    """,
    """
    CREATE TABLE experiments (
        id uuid PRIMARY KEY,
        config_json jsonb NOT NULL,
        config_hash text NOT NULL,
        status text NOT NULL,
        created_at timestamptz NOT NULL DEFAULT now()
    )
    """,
    """
    CREATE TABLE runs (
        id uuid PRIMARY KEY,
        experiment_id uuid REFERENCES experiments (id),
        parent_run_id uuid REFERENCES runs (id),
        scenario_id text NOT NULL,
        scenario_version integer NOT NULL,
        agent_id text NOT NULL,
        agent_version text NOT NULL,
        seed numeric(20, 0) NOT NULL,
        manifest jsonb NOT NULL,
        manifest_hash text NOT NULL,
        labels jsonb NOT NULL DEFAULT '{}'::jsonb,
        status text NOT NULL DEFAULT 'queued',
        verdict text,
        cancel_requested boolean NOT NULL DEFAULT false,
        fencing_token bigint NOT NULL DEFAULT 0,
        selected_attempt_id uuid,
        created_at timestamptz NOT NULL DEFAULT now(),
        closed_at timestamptz,
        FOREIGN KEY (scenario_id, scenario_version) REFERENCES scenario_versions (id, version),
        FOREIGN KEY (agent_id, agent_version) REFERENCES agent_versions (id, version),
        CONSTRAINT runs_status_check
            CHECK (status IN ('queued', 'running', 'evaluating', 'completed', 'error')),
        CONSTRAINT runs_verdict_check
            CHECK (verdict IS NULL OR verdict IN ('pass', 'fail', 'inconclusive')),
        CONSTRAINT runs_seed_u64_check CHECK (seed >= 0 AND seed < 18446744073709551616)
    )
    """,
    "CREATE INDEX runs_created_idx ON runs (created_at, id)",
    "CREATE INDEX runs_status_created_idx ON runs (status, created_at, id)",
    "CREATE INDEX runs_experiment_idx ON runs (experiment_id)",
    """
    CREATE TABLE run_attempts (
        id uuid PRIMARY KEY,
        run_id uuid NOT NULL REFERENCES runs (id),
        ordinal integer NOT NULL,
        fencing_token bigint NOT NULL,
        termination text,
        initial_state jsonb,
        final_state jsonb,
        final_state_hash text,
        next_seq bigint NOT NULL DEFAULT 1,
        chain_head text,
        created_at timestamptz NOT NULL DEFAULT now(),
        closed_at timestamptz,
        UNIQUE (run_id, ordinal)
    )
    """,
    # Added after both tables exist (runs <-> run_attempts reference each other).
    """
    ALTER TABLE runs ADD CONSTRAINT runs_selected_attempt_fk
        FOREIGN KEY (selected_attempt_id) REFERENCES run_attempts (id)
    """,
    """
    CREATE TABLE jobs (
        id uuid PRIMARY KEY,
        kind text NOT NULL,
        run_id uuid NOT NULL REFERENCES runs (id),
        attempt_id uuid REFERENCES run_attempts (id),
        dedupe_key text NOT NULL UNIQUE,
        status text NOT NULL DEFAULT 'queued',
        available_at timestamptz NOT NULL DEFAULT now(),
        lease_until timestamptz,
        owner text,
        fencing_token bigint NOT NULL DEFAULT 0,
        delivery_count integer NOT NULL DEFAULT 0,
        created_at timestamptz NOT NULL DEFAULT now(),
        CONSTRAINT jobs_kind_check CHECK (kind IN ('execute', 'grade', 'replay', 'export')),
        CONSTRAINT jobs_status_check CHECK (status IN ('queued', 'leased', 'done', 'error'))
    )
    """,
    """
    CREATE UNIQUE INDEX jobs_one_active_execute_per_run ON jobs (run_id)
        WHERE kind = 'execute' AND status IN ('queued', 'leased')
    """,
    """
    CREATE INDEX jobs_claim_idx ON jobs (status, available_at, lease_until)
        WHERE status IN ('queued', 'leased')
    """,
    """
    CREATE TABLE api_idempotency (
        principal text NOT NULL,
        route text NOT NULL,
        key text NOT NULL,
        request_hash text NOT NULL,
        resource_type text NOT NULL,
        resource_id uuid NOT NULL,
        created_at timestamptz NOT NULL DEFAULT now(),
        tombstoned_at timestamptz,
        PRIMARY KEY (principal, route, key)
    )
    """,
]


def upgrade() -> None:
    for statement in STATEMENTS:
        op.execute(statement)


def downgrade() -> None:
    # Destructive by nature; only for disposable development databases.
    for statement in (
        "DROP TABLE api_idempotency",
        "DROP TABLE jobs",
        "ALTER TABLE runs DROP CONSTRAINT runs_selected_attempt_fk",
        "DROP TABLE run_attempts",
        "DROP TABLE runs",
        "DROP TABLE experiments",
        "DROP TABLE agent_versions",
        "DROP TABLE scenario_versions",
        "DROP FUNCTION agentcrash_reject_change()",
    ):
        op.execute(statement)
