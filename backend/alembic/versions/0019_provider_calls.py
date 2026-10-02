"""provider_calls: a row per AI image provider call with its estimated cost (COST-4).

Idempotent (skipped if the table exists). The downgrade keeps the table (cost history).

Revision ID: 0019_provider_calls
Revises: 0018_consent_records
Create Date: 2026-10-02
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0019_provider_calls"
down_revision: Union[str, None] = "0018_consent_records"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_TABLE = "provider_calls"


def upgrade() -> None:
    if _TABLE in set(sa.inspect(op.get_bind()).get_table_names()):
        return
    op.create_table(
        _TABLE,
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("provider", sa.String(30), nullable=False),
        sa.Column("model", sa.String(80), nullable=True),
        sa.Column("outcome", sa.String(10), nullable=False),
        sa.Column("error_kind", sa.String(30), nullable=True),
        sa.Column("latency_ms", sa.Integer(), nullable=True),
        sa.Column("est_cost_paise", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("request_id", sa.String(64), nullable=True),
    )
    op.create_index("ix_provider_calls_created", _TABLE, ["created_at"])


def downgrade() -> None:
    """Deliberate no-op: cost history."""
