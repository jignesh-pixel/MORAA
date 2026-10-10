"""Catalog Pack v1.5: what a native WhatsApp Pay pack order buys, as a third product.

* whatsapp_payment_orders: + catalog_v1_5_packs (Catalog Pack v1.5 SKUs bought). A captured pack order grants these
  SKU credits (ledger sku "creative_pack_v1_5") next to the white-background and Catalog Pack ones.

The new column is nullable, so existing rows and inserts are unaffected. Idempotent.

Revision ID: 0025_catalog_v1_5_pack
Revises: 0024_whatsapp_pay_packs
Create Date: 2026-10-10
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0025_catalog_v1_5_pack"
down_revision: Union[str, None] = "0024_whatsapp_pay_packs"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_TABLE = "whatsapp_payment_orders"
_COLUMN = sa.Column("catalog_v1_5_packs", sa.Integer(), nullable=True)


def _columns(table: str) -> set:
    inspector = sa.inspect(op.get_bind())
    if table not in inspector.get_table_names():
        return set()
    return {c["name"] for c in inspector.get_columns(table)}


def upgrade() -> None:
    if _COLUMN.name not in _columns(_TABLE):
        op.add_column(_TABLE, _COLUMN)


def downgrade() -> None:
    if _COLUMN.name in _columns(_TABLE):
        op.drop_column(_TABLE, _COLUMN.name)
