"""whatsapp_ingestions.group_id: the bulk order a photo was gathered into (UX-1).

Idempotent (skipped if the column exists). The downgrade keeps the column (nullable, harmless).

Revision ID: 0020_ingestion_group_id
Revises: 0019_provider_calls
Create Date: 2026-10-03
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0020_ingestion_group_id"
down_revision: Union[str, None] = "0019_provider_calls"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_TABLE = "whatsapp_ingestions"


def upgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    if "group_id" in {c["name"] for c in inspector.get_columns(_TABLE)}:
        return
    op.add_column(_TABLE, sa.Column("group_id", sa.String(36), nullable=True))
    op.create_index("ix_whatsapp_ingestions_group_id", _TABLE, ["group_id"])


def downgrade() -> None:
    """Deliberate no-op: a nullable column."""
