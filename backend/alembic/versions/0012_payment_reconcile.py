"""Payment reconcile support (Q-3): backoff columns on WhatsApp Pay orders, and a table of Razorpay links.

* ``whatsapp_payment_orders``: ``next_check_at`` (+ index) and ``check_attempts`` so the sweep backs off
  instead of re-reading the same orders every few minutes and starving newer ones.
* ``razorpay_payment_links``: one row per recharge link we send, so a periodic sweep can ask Razorpay about
  links whose payment webhook never arrived.

Idempotent: every step is skipped if it already exists. The downgrade keeps everything (the data is payment
bookkeeping; the added columns are harmless on an older schema).

Revision ID: 0012_payment_reconcile
Revises: 0011_pending_payments
Create Date: 2026-10-02
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0012_payment_reconcile"
down_revision: Union[str, None] = "0011_pending_payments"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    insp = sa.inspect(op.get_bind())
    tables = set(insp.get_table_names())

    if "whatsapp_payment_orders" in tables:
        cols = {c["name"] for c in insp.get_columns("whatsapp_payment_orders")}
        if "next_check_at" not in cols:
            op.add_column(
                "whatsapp_payment_orders",
                sa.Column("next_check_at", sa.DateTime(timezone=True), nullable=True),
            )
            op.create_index("ix_whatsapp_payment_orders_next_check_at", "whatsapp_payment_orders", ["next_check_at"])
        if "check_attempts" not in cols:
            op.add_column(
                "whatsapp_payment_orders",
                sa.Column("check_attempts", sa.Integer(), nullable=False, server_default="0"),
            )

    if "razorpay_payment_links" not in tables:
        op.create_table(
            "razorpay_payment_links",
            sa.Column("id", sa.String(36), primary_key=True),
            sa.Column("link_id", sa.String(64), nullable=False, unique=True, comment="Razorpay plink_... id"),
            sa.Column("whatsapp_id", sa.String(100), nullable=False),
            sa.Column("amount_rupees", sa.Integer(), nullable=False),
            sa.Column("status", sa.String(20), nullable=False),
            sa.Column("next_check_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("check_attempts", sa.Integer(), nullable=False),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        )
        op.create_index("ix_razorpay_payment_links_whatsapp_id", "razorpay_payment_links", ["whatsapp_id"])
        op.create_index("ix_razorpay_payment_links_status", "razorpay_payment_links", ["status"])
        op.create_index("ix_razorpay_payment_links_next_check_at", "razorpay_payment_links", ["next_check_at"])


def downgrade() -> None:
    """Deliberate no-op: payment bookkeeping is kept, and the extra columns are harmless on an older schema."""
