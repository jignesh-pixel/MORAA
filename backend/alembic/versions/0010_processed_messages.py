"""processed_messages: one row per handled Meta message id, so a re-delivered text or button message is ignored.

Idempotent (skipped if the table exists). The downgrade drops the table: it only holds duplicate-delivery
bookkeeping, no customer or money data.

Revision ID: 0010_processed_messages
Revises: 0009_wallet_ledger
Create Date: 2026-10-02
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0010_processed_messages"
down_revision: Union[str, None] = "0009_wallet_ledger"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_TABLE = "processed_messages"


def upgrade() -> None:
    if _TABLE in set(sa.inspect(op.get_bind()).get_table_names()):
        return
    op.create_table(
        _TABLE,
        sa.Column("message_id", sa.String(255), primary_key=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_processed_messages_created_at", _TABLE, ["created_at"])


def downgrade() -> None:
    if _TABLE in set(sa.inspect(op.get_bind()).get_table_names()):
        op.drop_table(_TABLE)
