"""scheduler_leases: one owner per periodic job across server processes (ARC-2).

Idempotent (skipped if the table exists). The downgrade keeps the table (tiny, harmless).

Revision ID: 0017_scheduler_leases
Revises: 0016_outbox_jobs
Create Date: 2026-10-02
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0017_scheduler_leases"
down_revision: Union[str, None] = "0016_outbox_jobs"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_TABLE = "scheduler_leases"


def upgrade() -> None:
    if _TABLE in set(sa.inspect(op.get_bind()).get_table_names()):
        return
    op.create_table(
        _TABLE,
        sa.Column("name", sa.String(60), primary_key=True),
        sa.Column("holder", sa.String(64), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )


def downgrade() -> None:
    """Deliberate no-op: a tiny table, harmless to keep."""
