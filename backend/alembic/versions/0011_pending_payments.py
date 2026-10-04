"""pending_payments: paid Razorpay payments with no usable payer phone, parked for manual review.

Idempotent (skipped if the table exists). The downgrade keeps the table (it holds unreviewed money).

Revision ID: 0011_pending_payments
Revises: 0010_processed_messages
Create Date: 2026-10-02
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0011_pending_payments"
down_revision: Union[str, None] = "0010_processed_messages"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_TABLE = "pending_payments"


def upgrade() -> None:
    if _TABLE in set(sa.inspect(op.get_bind()).get_table_names()):
        return
    op.create_table(
        _TABLE,
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("payment_id", sa.String(64), nullable=False, unique=True, comment="Razorpay pay_... id"),
        sa.Column("amount_rupees", sa.Integer(), nullable=False),
        sa.Column("currency", sa.String(8), nullable=False),
        sa.Column("event", sa.String(40), nullable=True, comment="Razorpay event that reported it"),
        sa.Column("payer_hint", sa.String(100), nullable=True, comment="Whatever id Razorpay gave (e.g. cust_...)"),
        sa.Column("reason", sa.String(40), nullable=False, comment="missing_phone | not_a_phone_number"),
        sa.Column("status", sa.String(20), nullable=False),
        sa.Column("credited_whatsapp_id", sa.String(100), nullable=True),
        sa.Column("note", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("resolved_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index("ix_pending_payments_status", _TABLE, ["status"])


def downgrade() -> None:
    """Deliberate no-op: parked payments are real money still waiting for a person to credit them."""
