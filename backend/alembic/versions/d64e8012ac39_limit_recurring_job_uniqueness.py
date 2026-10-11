"""Limit recurring-job uniqueness to insights and digests.

Revision ID: d64e8012ac39
Revises: b3c1d9e0a7f2
"""

from alembic import op
import sqlalchemy as sa

revision = "d64e8012ac39"
down_revision = "b3c1d9e0a7f2"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.drop_index("uq_jobs_single_pending_recurring", table_name="jobs")
    op.create_index(
        "uq_jobs_single_pending_recurring",
        "jobs",
        ["business_id", "kind"],
        unique=True,
        postgresql_where=sa.text(
            "status = 'pending' AND case_id IS NULL AND kind IN ('insights', 'digest')"
        ),
    )


def downgrade() -> None:
    op.drop_index("uq_jobs_single_pending_recurring", table_name="jobs")
    op.create_index(
        "uq_jobs_single_pending_recurring",
        "jobs",
        ["business_id", "kind"],
        unique=True,
        postgresql_where=sa.text("status = 'pending' AND case_id IS NULL"),
    )
