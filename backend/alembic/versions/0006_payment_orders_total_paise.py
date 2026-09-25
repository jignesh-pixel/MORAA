"""Additive migration: whatsapp_payment_orders.total_paise.

The whatsapp_payment_orders table was created in some databases by
init_db()/create_all from an earlier model revision that had no
total_paise column, and 0005 (which skips an existing table) was then
recorded as applied. The WhatsApp Pay code reads and writes total_paise,
so this adds the column where it is missing. Idempotent: a no-op where the
column already exists (e.g. a fresh database built by 0005).

server_default 0 keeps the add safe if rows exist; such rows can never be
credited because 0 never equals a real paid amount.

Revision ID: 0006_payment_orders_total_paise
Revises: 0005_whatsapp_payment_orders
Create Date: 2026-09-25
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0006_payment_orders_total_paise"
down_revision: Union[str, None] = "0005_whatsapp_payment_orders"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_TABLE = "whatsapp_payment_orders"
_COLUMN = "total_paise"


def _columns() -> set:
    inspector = sa.inspect(op.get_bind())
    if _TABLE not in inspector.get_table_names():
        return set()
    return {c["name"] for c in inspector.get_columns(_TABLE)}


def upgrade() -> None:
    columns = _columns()
    if columns and _COLUMN not in columns:
        op.add_column(_TABLE, sa.Column(_COLUMN, sa.Integer(), nullable=False, server_default="0"))


def downgrade() -> None:
    # 0005 creates total_paise itself on fresh databases, so the column is
    # intentionally kept on downgrade (dropping it would break 0005's schema).
    pass
