"""Phase 8: SKU credit ledger with running balance, price metadata, order/payment-link/invoice Drive columns.

0022 is already on main, so everything Phase 8 adds on top of it lives here:

* customer_sku_credits is rebuilt with ``balance_after`` (CHECK >= 0) and ``expires_at``. Nothing writes the table
  before this release, so it is empty; the rebuild refuses if it is not (SQLite cannot add a CHECK to a table).
* price_settings: + effective_from, changed_by.
* whatsapp_ingestions: + credit_source, delivery_channel, drive_file_id.
* razorpay_payment_links: + purpose, units.
* invoice_records: + drive_file_id, drive_link.

Every new column is nullable, so existing rows and inserts are unaffected. Idempotent. The downgrade reverses it all
and, like 0022, refuses while the credit ledger holds rows.

Revision ID: 0023_sku_packs_drive
Revises: 0022_phase8_foundations
Create Date: 2026-10-07
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0023_sku_packs_drive"
down_revision: Union[str, None] = "0022_phase8_foundations"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_LEDGER = "customer_sku_credits"

_NEW_COLUMNS = (
    ("price_settings", sa.Column("effective_from", sa.DateTime(timezone=True), nullable=True)),
    ("price_settings", sa.Column("changed_by", sa.String(100), nullable=True)),
    ("whatsapp_ingestions", sa.Column("credit_source", sa.String(16), nullable=True)),
    ("whatsapp_ingestions", sa.Column("delivery_channel", sa.String(16), nullable=True)),
    ("whatsapp_ingestions", sa.Column("drive_file_id", sa.String(200), nullable=True)),
    ("razorpay_payment_links", sa.Column("purpose", sa.String(16), nullable=True)),
    ("razorpay_payment_links", sa.Column("units", sa.Integer(), nullable=True)),
    ("invoice_records", sa.Column("drive_file_id", sa.String(200), nullable=True)),
    ("invoice_records", sa.Column("drive_link", sa.String(500), nullable=True)),
)


def _columns(table: str) -> set:
    inspector = sa.inspect(op.get_bind())
    if table not in inspector.get_table_names():
        return set()
    return {c["name"] for c in inspector.get_columns(table)}


def _drop_empty_ledger() -> None:
    if _LEDGER not in sa.inspect(op.get_bind()).get_table_names():
        return
    if op.get_bind().execute(sa.text(f"SELECT 1 FROM {_LEDGER} LIMIT 1")).first() is not None:
        raise RuntimeError(f"{_LEDGER} holds customers' credits; refusing to rebuild it. Settle or export them first.")
    op.drop_index("ix_customer_sku_credits_customer_id", table_name=_LEDGER)
    op.drop_table(_LEDGER)


def _create_ledger(full: bool) -> None:
    columns = [
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("customer_id", sa.String(36), sa.ForeignKey("customers.id"), nullable=False),
        sa.Column("sku", sa.String(64), nullable=False),
        sa.Column("action", sa.String(32), nullable=False, comment="purchase, consume, refund, expire, clawback"),
        sa.Column("quantity", sa.Integer(), nullable=False, comment="Signed credits: added +, used -"),
    ]
    if full:
        columns.append(sa.Column("balance_after", sa.Integer(), nullable=False, comment="Customer's credits right after this row"))
    columns += [
        sa.Column("reference_id", sa.String(64), nullable=False, comment="Payment id, order id or expiry key"),
    ]
    if full:
        columns.append(sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True, comment="Purchase rows: valid until"))
    columns += [
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("quantity <> 0", name="ck_customer_sku_credits_quantity_nonzero"),
        sa.UniqueConstraint("reference_id", "action", name="uq_customer_sku_credits_reference_action"),
    ]
    if full:
        columns.append(sa.CheckConstraint("balance_after >= 0", name="ck_customer_sku_credits_balance_nonneg"))
    op.create_table(_LEDGER, *columns)
    op.create_index("ix_customer_sku_credits_customer_id", _LEDGER, ["customer_id"])


def upgrade() -> None:
    if "balance_after" not in _columns(_LEDGER):
        _drop_empty_ledger()
        _create_ledger(full=True)
    for table, column in _NEW_COLUMNS:
        if column.name not in _columns(table):
            op.add_column(table, column)


def downgrade() -> None:
    for table, column in reversed(_NEW_COLUMNS):
        if column.name in _columns(table):
            op.drop_column(table, column.name)
    if "balance_after" in _columns(_LEDGER):
        _drop_empty_ledger()
        _create_ledger(full=False)
