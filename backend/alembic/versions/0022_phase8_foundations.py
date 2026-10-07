"""Phase 8 foundations: wider status/product/resource columns, customer email and Drive ids, prices, SKU credits.

* whatsapp_ingestions.status and .product_code: VARCHAR(20) -> VARCHAR(64); audit_logs.resource_id: VARCHAR(36) ->
  VARCHAR(64). On PostgreSQL widening a VARCHAR is a catalog-only change (no table rewrite, indexes stay valid).
  SQLite does not enforce VARCHAR lengths, so nothing is altered there.
* customers: email, drive_folder_id, drive_images_folder_id, drive_invoices_folder_id, drive_sheet_id,
  drive_shared_to. All nullable with no default, so existing rows and every current insert are unaffected.
* New tables price_settings and customer_sku_credits (append-only, UNIQUE(reference_id, action)).

Idempotent: each step is skipped when already done. The downgrade reverses everything, but refuses while
customer_sku_credits holds rows (credits cannot be rebuilt from anything else), and narrowing a column fails
(rolling back, losing nothing) while a longer value is stored in it.

Revision ID: 0022_phase8_foundations
Revises: 0021_chat_dashboard
Create Date: 2026-10-07
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0022_phase8_foundations"
down_revision: Union[str, None] = "0021_chat_dashboard"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

# (table, column, old length, nullable)
_WIDENED = (
    ("whatsapp_ingestions", "status", 20, False),
    ("whatsapp_ingestions", "product_code", 20, True),
    ("audit_logs", "resource_id", 36, True),
)
_NEW_LENGTH = 64

_CUSTOMER_COLUMNS = (
    ("email", 320),
    ("drive_folder_id", 200),
    ("drive_images_folder_id", 200),
    ("drive_invoices_folder_id", 200),
    ("drive_sheet_id", 200),
    ("drive_shared_to", 320),
)


def _inspector():
    return sa.inspect(op.get_bind())


def _length(table: str, column: str):
    for col in _inspector().get_columns(table):
        if col["name"] == column:
            return getattr(col["type"], "length", None)
    return None


def _resize(new_length_for) -> None:
    if op.get_bind().dialect.name != "postgresql":
        return  # SQLite ignores VARCHAR lengths
    for table, column, old, nullable in _WIDENED:
        current, wanted = _length(table, column), new_length_for(old)
        if current is not None and current != wanted:
            op.alter_column(
                table, column,
                type_=sa.String(wanted), existing_type=sa.String(current), existing_nullable=nullable,
            )


def upgrade() -> None:
    _resize(lambda old: _NEW_LENGTH)

    existing = {c["name"] for c in _inspector().get_columns("customers")}
    for name, length in _CUSTOMER_COLUMNS:
        if name not in existing:
            op.add_column("customers", sa.Column(name, sa.String(length), nullable=True))

    tables = set(_inspector().get_table_names())
    if "price_settings" not in tables:
        op.create_table(
            "price_settings",
            sa.Column("sku", sa.String(64), primary_key=True),
            sa.Column("price_rupees", sa.Integer(), nullable=False, comment="Whole rupees"),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
            sa.CheckConstraint("price_rupees >= 0", name="ck_price_settings_price_nonneg"),
        )
    if "customer_sku_credits" not in tables:
        op.create_table(
            "customer_sku_credits",
            sa.Column("id", sa.String(36), primary_key=True),
            sa.Column("customer_id", sa.String(36), sa.ForeignKey("customers.id"), nullable=False),
            sa.Column("sku", sa.String(64), nullable=False),
            sa.Column("action", sa.String(32), nullable=False, comment="purchase, consume, refund"),
            sa.Column("quantity", sa.Integer(), nullable=False, comment="Signed credits: added +, used -"),
            sa.Column("reference_id", sa.String(64), nullable=False, comment="Payment id or order id"),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.CheckConstraint("quantity <> 0", name="ck_customer_sku_credits_quantity_nonzero"),
            sa.UniqueConstraint("reference_id", "action", name="uq_customer_sku_credits_reference_action"),
        )
        op.create_index("ix_customer_sku_credits_customer_id", "customer_sku_credits", ["customer_id"])


def downgrade() -> None:
    bind = op.get_bind()
    tables = set(_inspector().get_table_names())
    if "customer_sku_credits" in tables:
        if bind.execute(sa.text("SELECT 1 FROM customer_sku_credits LIMIT 1")).first() is not None:
            raise RuntimeError(
                "customer_sku_credits holds customers' credits; refusing to drop it. "
                "Settle or export the credits first."
            )
        op.drop_index("ix_customer_sku_credits_customer_id", table_name="customer_sku_credits")
        op.drop_table("customer_sku_credits")
    if "price_settings" in tables:
        op.drop_table("price_settings")

    existing = {c["name"] for c in _inspector().get_columns("customers")}
    for name, _length_unused in reversed(_CUSTOMER_COLUMNS):
        if name in existing:
            op.drop_column("customers", name)

    _resize(lambda old: old)
