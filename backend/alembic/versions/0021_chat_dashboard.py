"""chat_messages, order_outputs, invoice_records: the data behind the WhatsApp chat dashboard.

Idempotent (each table is skipped if it exists). The downgrade keeps the tables (they hold audit history).

Revision ID: 0021_chat_dashboard
Revises: 0020_ingestion_group_id
Create Date: 2026-10-03
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0021_chat_dashboard"
down_revision: Union[str, None] = "0020_ingestion_group_id"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    existing = set(sa.inspect(op.get_bind()).get_table_names())
    if "chat_messages" not in existing:
        op.create_table(
            "chat_messages",
            sa.Column("id", sa.String(36), primary_key=True),
            sa.Column("customer_phone", sa.String(100), nullable=False),
            sa.Column("direction", sa.String(3), nullable=False),
            sa.Column("msg_type", sa.String(30), nullable=False),
            sa.Column("text", sa.Text(), nullable=True),
            sa.Column("wa_message_id", sa.String(200), nullable=True),
            sa.Column("ingestion_id", sa.String(36), nullable=True),
            sa.Column("image_id", sa.String(36), nullable=True),
            sa.Column("output_id", sa.String(36), nullable=True),
            sa.Column("meta_media_id", sa.String(200), nullable=True),
            sa.Column("drive_file_id", sa.String(200), nullable=True),
            sa.Column("drive_link", sa.String(500), nullable=True),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.UniqueConstraint("direction", "wa_message_id", name="uq_chat_messages_direction_wa_id"),
        )
        op.create_index("ix_chat_messages_customer_time", "chat_messages", ["customer_phone", "created_at"])
        op.create_index("ix_chat_messages_ingestion_id", "chat_messages", ["ingestion_id"])
        op.create_index("ix_chat_messages_created_at", "chat_messages", ["created_at"])
    if "order_outputs" not in existing:
        op.create_table(
            "order_outputs",
            sa.Column("id", sa.String(36), primary_key=True),
            sa.Column("ingestion_id", sa.String(36), nullable=False),
            sa.Column("style", sa.String(80), nullable=True),
            sa.Column("position", sa.Integer(), nullable=False),
            sa.Column("file_path", sa.String(500), nullable=True),
            sa.Column("mime_type", sa.String(50), nullable=False),
            sa.Column("size_bytes", sa.Integer(), nullable=True),
            sa.Column("meta_media_id", sa.String(200), nullable=True),
            sa.Column("drive_file_id", sa.String(200), nullable=True),
            sa.Column("drive_link", sa.String(500), nullable=True),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        )
        op.create_index("ix_order_outputs_ingestion_id", "order_outputs", ["ingestion_id"])
        op.create_index("ix_order_outputs_meta_media_id", "order_outputs", ["meta_media_id"])
    if "invoice_records" not in existing:
        op.create_table(
            "invoice_records",
            sa.Column("id", sa.String(36), primary_key=True),
            sa.Column("payment_id", sa.String(100), nullable=False),
            sa.Column("customer_phone", sa.String(100), nullable=False),
            sa.Column("amount_rupees", sa.Integer(), nullable=False),
            sa.Column("status", sa.String(10), nullable=False),
            sa.Column("erpnext_invoice", sa.String(100), nullable=True),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
            sa.UniqueConstraint("payment_id", name="uq_invoice_records_payment_id"),
        )
        op.create_index("ix_invoice_records_customer_phone", "invoice_records", ["customer_phone"])


def downgrade() -> None:
    """Deliberate no-op: audit history."""
