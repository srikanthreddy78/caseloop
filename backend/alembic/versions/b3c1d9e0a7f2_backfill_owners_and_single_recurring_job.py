"""backfill owners and single recurring job

Revision ID: b3c1d9e0a7f2
Revises: f474ee7e4239
Create Date: 2026-10-11 16:40:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'b3c1d9e0a7f2'
down_revision: Union[str, Sequence[str], None] = 'f474ee7e4239'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

# Businesses created before team members existed have nobody who can sign in: the hand-off
# address becomes the owner, lowercased because sign-in compares lowercased addresses.
OWNER_BACKFILL_SQL = """
INSERT INTO team_members (id, business_id, email, name, role)
SELECT gen_random_uuid(), b.id, lower(b.escalation_email), '', 'owner'
FROM businesses b
WHERE NOT EXISTS (SELECT 1 FROM team_members m WHERE m.business_id = b.id)
"""

# Startup races booked the same recurring job twice; keep the earliest pending one per kind.
DUPLICATE_RECURRING_SQL = """
UPDATE jobs SET status = 'cancelled'
WHERE status = 'pending' AND case_id IS NULL AND id NOT IN (
    SELECT DISTINCT ON (business_id, kind) id FROM jobs
    WHERE status = 'pending' AND case_id IS NULL
    ORDER BY business_id, kind, due_at, created_at
)
"""


def upgrade() -> None:
    """Upgrade schema."""
    op.execute(OWNER_BACKFILL_SQL)
    op.execute(DUPLICATE_RECURRING_SQL)
    op.create_index(
        'uq_jobs_single_pending_recurring',
        'jobs',
        ['business_id', 'kind'],
        unique=True,
        postgresql_where=sa.text("status = 'pending' AND case_id IS NULL"),
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index('uq_jobs_single_pending_recurring', table_name='jobs')
