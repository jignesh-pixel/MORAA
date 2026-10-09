"""Native WhatsApp Pay for SKU packs: what an order_details order buys.

* whatsapp_payment_orders: + purpose ("sku_pack"; NULL = wallet recharge, as every existing row), white_units,
  creative_packs. A captured pack order grants these SKU credits instead of crediting the wallet.

Every new column is nullable, so existing rows and inserts are unaffected. Idempotent.

Revision ID: 0024_whatsapp_pay_packs
Revises: 0023_sku_packs_drive
Create Date: 2026-10-09
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0024_whatsapp_pay_packs"
down_revision: Union[str, None] = "0023_sku_packs_drive"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_TABLE = "whatsapp_payment_orders"
_NEW_COLUMNS = (
    sa.Column("purpose", sa.String(16), nullable=True),
    sa.Column("white_units", sa.Integer(), nullable=True),
    sa.Column("creative_packs", sa.Integer(), nullable=True),
)


def _columns(table: str) -> set:
    inspector = sa.inspect(op.get_bind())
    if table not in inspector.get_table_names():
        return set()
    return {c["name"] for c in inspector.get_columns(table)}


def upgrade() -> None:
    for column in _NEW_COLUMNS:
        if column.name not in _columns(_TABLE):
            op.add_column(_TABLE, column)


def downgrade() -> None:
    for column in reversed(_NEW_COLUMNS):
        if column.name in _columns(_TABLE):
            op.drop_column(_TABLE, column.name)
