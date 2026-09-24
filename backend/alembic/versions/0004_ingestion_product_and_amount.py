"""Additive migration: product + charged amount on whatsapp_ingestions.

Adds two NULLABLE columns so every paid WhatsApp order records what was
bought and how much was actually debited:

    product_code    VARCHAR(20) NULL   ('PACK_1' | 'WHITE_BG')
    amount_charged  INTEGER     NULL   (whole Indian Rupees)

Existing rows keep NULL in both columns; application code treats NULL as a
legacy E-Com Pack 1 order refunded at the configured Pack 1 price, which is
exactly today's behaviour. No backfill, no rewrite, no data loss.

Strictly additive and idempotent (same pattern as 0002): each add_column is
guarded by an inspector check, so a database whose table already has the
columns (e.g. created by init_db()/create_all) is a no-op. downgrade drops
only these two columns.

Revision ID: 0004_ingestion_product
Revises: 0003_audit_money_once
Create Date: 2026-09-24
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0004_ingestion_product"
down_revision: Union[str, None] = "0003_audit_money_once"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_TABLE = "whatsapp_ingestions"


def _existing_columns(table_name: str) -> set:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if table_name.lower() not in {name.lower() for name in inspector.get_table_names()}:
        return set()
    return {column["name"].lower() for column in inspector.get_columns(table_name)}


def upgrade() -> None:
    columns = _existing_columns(_TABLE)
    if not columns:
        return

    if "product_code" not in columns:
        op.add_column(
            _TABLE,
            sa.Column("product_code", sa.String(length=20), nullable=True),
        )

    if "amount_charged" not in columns:
        op.add_column(
            _TABLE,
            sa.Column("amount_charged", sa.Integer(), nullable=True),
        )


def downgrade() -> None:
    columns = _existing_columns(_TABLE)
    # batch mode so SQLite (no native DROP COLUMN on older versions) works too
    with op.batch_alter_table(_TABLE) as batch_op:
        if "amount_charged" in columns:
            batch_op.drop_column("amount_charged")
        if "product_code" in columns:
            batch_op.drop_column("product_code")
