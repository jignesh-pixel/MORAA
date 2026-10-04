"""consent_records: who agreed to which version of the data notice, and when (PRIV-2).

Idempotent (skipped if the table exists). The downgrade keeps the table: it is evidence of consent.

Revision ID: 0018_consent_records
Revises: 0017_scheduler_leases
Create Date: 2026-10-02
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0018_consent_records"
down_revision: Union[str, None] = "0017_scheduler_leases"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_TABLE = "consent_records"


def upgrade() -> None:
    if _TABLE in set(sa.inspect(op.get_bind()).get_table_names()):
        return
    op.create_table(
        _TABLE,
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("whatsapp_id", sa.String(100), nullable=False),
        sa.Column("version", sa.String(20), nullable=False),
        sa.Column("accepted_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("whatsapp_id", "version", name="uq_consent_whatsapp_version"),
    )
    op.create_index("ix_consent_records_whatsapp_id", _TABLE, ["whatsapp_id"])


def downgrade() -> None:
    """Deliberate no-op: the table is evidence of consent."""
