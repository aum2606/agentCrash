"""Experiment matrix slots (M5).

Revision ID: 0005
Revises: 0004
Additive (expand-only): ``experiment_slots`` pairs each experiment cell and agent role with its
child run (CONTRACTS section 8; ``scenario_id`` and ``scenario_version`` are additions so a slot is
readable without joining the run). Slots are immutable once written.
"""

from alembic import op

revision = "0005"
down_revision = "0004"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE experiment_slots (
            experiment_id uuid NOT NULL REFERENCES experiments (id),
            scenario_hash text NOT NULL,
            scenario_id text NOT NULL,
            scenario_version integer NOT NULL,
            seed numeric(20, 0) NOT NULL,
            trial_index integer NOT NULL,
            agent_role text NOT NULL,
            run_id uuid NOT NULL UNIQUE REFERENCES runs (id),
            PRIMARY KEY (experiment_id, scenario_hash, seed, trial_index, agent_role),
            CONSTRAINT experiment_slots_role_check
                CHECK (agent_role IN ('baseline', 'candidate')),
            CONSTRAINT experiment_slots_trial_check CHECK (trial_index >= 1)
        )
        """
    )
    op.execute(
        """
        CREATE TRIGGER experiment_slots_immutable
            BEFORE UPDATE OR DELETE ON experiment_slots
            FOR EACH ROW EXECUTE FUNCTION agentcrash_reject_change()
        """
    )


def downgrade() -> None:
    op.execute("DROP TABLE experiment_slots")
