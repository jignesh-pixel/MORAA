"""Additive migration: whatsapp_payment_orders (native WhatsApp Pay orders).

Creates one new table; no existing table is touched. Guarded by an
inspector check so a database where init_db()/create_all already created
the table is a no-op. downgrade drops only this table.

Revision ID: 0005_whatsapp_payment_orders
Revises: 0004_ingestion_product
Create Date: 2026-09-25
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0005_whatsapp_payment_orders"
down_revision: Union[str, None] = "0004_ingestion_product"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_TABLE = "whatsapp_payment_orders"


def upgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    if _TABLE in inspector.get_table_names():
        return
    op.create_table(
        _TABLE,
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("reference_id", sa.String(length=35), nullable=False),
        sa.Column("whatsapp_id", sa.String(length=100), nullable=False),
        sa.Column("amount_rupees", sa.Integer(), nullable=False),
        sa.Column("total_paise", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column("configuration_name", sa.String(length=60), nullable=True),
        sa.Column("pg_order_id", sa.String(length=64), nullable=True),
        sa.Column("pg_payment_id", sa.String(length=64), nullable=True),
        sa.Column("credited", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("fallback_sent", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_whatsapp_payment_orders_reference_id", _TABLE, ["reference_id"], unique=True)
    op.create_index("ix_whatsapp_payment_orders_whatsapp_id", _TABLE, ["whatsapp_id"])
    op.create_index("ix_whatsapp_payment_orders_status", _TABLE, ["status"])


def downgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    if _TABLE not in inspector.get_table_names():
        return
    op.drop_index("ix_whatsapp_payment_orders_status", table_name=_TABLE)
    op.drop_index("ix_whatsapp_payment_orders_whatsapp_id", table_name=_TABLE)
    op.drop_index("ix_whatsapp_payment_orders_reference_id", table_name=_TABLE)
    op.drop_table(_TABLE)
