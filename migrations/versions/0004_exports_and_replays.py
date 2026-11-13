"""Artifacts, exports and replays (M4).

Revision ID: 0004
Revises: 0003
Additive (expand-only): four new tables and one nullable column on ``jobs``. ``exports`` and
``replays`` hold the state of export and strict-replay jobs; ``artifacts`` and ``artifact_refs``
are the content-addressed bundle index and its reference/pin metadata (CONTRACTS section 8;
``exports``, ``replays.run_id/total_events/error_code`` and ``jobs.subject_id`` are additions).
"""

from alembic import op

revision = "0004"
down_revision = "0003"
branch_labels = None
depends_on = None

STATUS = "('queued', 'running', 'completed', 'error')"


def upgrade() -> None:
    op.execute("ALTER TABLE jobs ADD COLUMN subject_id uuid")
    op.execute(
        """
        CREATE TABLE artifacts (
            hash text PRIMARY KEY,
            kind text NOT NULL,
            byte_size bigint NOT NULL,
            relative_storage_key text NOT NULL,
            created_at timestamptz NOT NULL DEFAULT now(),
            CONSTRAINT artifacts_size_check CHECK (byte_size >= 0)
        )
        """
    )
    op.execute(
        """
        CREATE TABLE artifact_refs (
            owner_type text NOT NULL,
            owner_id uuid NOT NULL,
            hash text NOT NULL REFERENCES artifacts (hash),
            pin_until timestamptz,
            PRIMARY KEY (owner_type, owner_id, hash)
        )
        """
    )
    op.execute("CREATE INDEX artifact_refs_hash_idx ON artifact_refs (hash)")
    op.execute(
        f"""
        CREATE TABLE exports (
            id uuid PRIMARY KEY,
            run_id uuid NOT NULL REFERENCES runs (id),
            attempt_id uuid NOT NULL REFERENCES run_attempts (id),
            status text NOT NULL DEFAULT 'queued',
            pin boolean NOT NULL,
            artifact_hash text REFERENCES artifacts (hash),
            error_code text,
            created_at timestamptz NOT NULL DEFAULT now(),
            closed_at timestamptz,
            CONSTRAINT exports_status_check CHECK (status IN {STATUS})
        )
        """
    )
    op.execute(
        f"""
        CREATE TABLE replays (
            id uuid PRIMARY KEY,
            run_id uuid NOT NULL REFERENCES runs (id),
            source_attempt_id uuid NOT NULL REFERENCES run_attempts (id),
            status text NOT NULL DEFAULT 'queued',
            adapter_hash text,
            consumed_events integer,
            total_events integer,
            divergence jsonb,
            error_code text,
            created_at timestamptz NOT NULL DEFAULT now(),
            closed_at timestamptz,
            CONSTRAINT replays_status_check CHECK (status IN {STATUS})
        )
        """
    )


def downgrade() -> None:
    for table in ("replays", "exports", "artifact_refs", "artifacts"):
        op.execute(f"DROP TABLE {table}")
    op.execute("ALTER TABLE jobs DROP COLUMN subject_id")
