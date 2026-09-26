"""Additive migration: customers.is_gst_verified.

One NOT NULL boolean with server default false, so existing rows become
"not verified" and nothing else changes. Idempotent (skipped when the column
already exists, e.g. created by init_db()/create_all). downgrade drops it.

Revision ID: 0007_customer_is_gst_verified
Revises: 0006_payment_orders_total_paise
Create Date: 2026-09-26
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0007_customer_is_gst_verified"
down_revision: Union[str, None] = "0006_payment_orders_total_paise"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_TABLE = "customers"
_COLUMN = "is_gst_verified"


def _columns() -> set:
    inspector = sa.inspect(op.get_bind())
    if _TABLE not in inspector.get_table_names():
        return set()
    return {c["name"] for c in inspector.get_columns(_TABLE)}


def upgrade() -> None:
    columns = _columns()
    if columns and _COLUMN not in columns:
        op.add_column(_TABLE, sa.Column(_COLUMN, sa.Boolean(), nullable=False, server_default=sa.false()))


def downgrade() -> None:
    if _COLUMN in _columns():
        op.drop_column(_TABLE, _COLUMN)
