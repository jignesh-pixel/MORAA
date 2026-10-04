"""outbox_jobs: durable background jobs (ops forwards, invoices) that survive a restart (Q-5).

Idempotent (skipped if the table exists). The downgrade keeps the table: it may hold jobs not yet delivered.

Revision ID: 0016_outbox_jobs
Revises: 0015_auth_hardening
Create Date: 2026-10-02
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0016_outbox_jobs"
down_revision: Union[str, None] = "0015_auth_hardening"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_TABLE = "outbox_jobs"


def upgrade() -> None:
    if _TABLE in set(sa.inspect(op.get_bind()).get_table_names()):
        return
    op.create_table(
        _TABLE,
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("kind", sa.String(40), nullable=False),
        sa.Column("dedupe_key", sa.String(120), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.Column("status", sa.String(10), nullable=False),
        sa.Column("attempts", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("next_attempt_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("dedupe_key", name="uq_outbox_jobs_dedupe_key"),
    )
    op.create_index("ix_outbox_jobs_due", _TABLE, ["status", "next_attempt_at"])


def downgrade() -> None:
    """Deliberate no-op: the table may hold undelivered jobs."""
