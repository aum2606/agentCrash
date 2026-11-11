"""Failure classification stored with each evaluation.

Revision ID: 0003
Revises: 0002
Additive (expand-only): two columns on ``evaluations``. ``primary_reason`` is NULL for a passing
run (and for rows written before this revision); ``findings`` defaults to an empty list.
"""

from alembic import op

revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TABLE evaluations ADD COLUMN primary_reason text")
    op.execute("ALTER TABLE evaluations ADD COLUMN findings jsonb NOT NULL DEFAULT '[]'::jsonb")


def downgrade() -> None:
    op.execute("ALTER TABLE evaluations DROP COLUMN findings")
    op.execute("ALTER TABLE evaluations DROP COLUMN primary_reason")
