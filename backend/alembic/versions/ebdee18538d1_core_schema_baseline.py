"""Core schema baseline: create missing core tables and indexes.

Revision ID: ebdee18538d1
Revises: 0002_customer_wallet
Create Date: 2026-09-13 13:12:32.641866

WHAT THIS REVISION DOES
-----------------------
This revision was originally auto-generated as a destructive "drop the
whole legacy schema" migration (13 x ``op.drop_index`` +
``op.drop_table`` pairs).  That behaviour is wrong on a fresh database
(nothing to drop → ``UndefinedObject``) and dangerous on an existing one
(it would delete every table).  It has therefore been rewritten as an
**idempotent, non-destructive baseline**:

    * creates any of the core application tables that do not exist yet,
      so a fresh Supabase/PostgreSQL database ends up with the complete
      schema after ``alembic upgrade head``
    * creates every ORM index (``ix_*``) only when it is missing, so
      databases bootstrapped by ``init_db()`` / ``create_all()`` are not
      touched twice
    * NEVER drops a table or an index — ``downgrade()`` is a deliberate
      no-op for the same reason

Column definitions mirror the SQLAlchemy ORM models exactly (see
``backend/app/models/``).
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "ebdee18538d1"
down_revision: Union[str, None] = "0002_customer_wallet"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

def _existing_tables() -> set:
    """Return the set of existing table names (case-insensitive)."""
    inspector = sa.inspect(op.get_bind())
    return {name.lower() for name in inspector.get_table_names()}


def _existing_indexes(table_name: str) -> set:
    """Return the set of existing index names on ``table_name`` (case-insensitive)."""
    inspector = sa.inspect(op.get_bind())
    return {index["name"].lower() for index in inspector.get_indexes(table_name)}


def _create_table_if_missing(table_name: str, table_factory) -> None:
    """Run ``table_factory()`` (a zero-arg ``op.create_table`` wrapper) only when absent."""
    if table_name.lower() not in _existing_tables():
        table_factory()


def _create_index_if_missing(index_name: str, table_name: str, columns: list, unique: bool) -> None:
    """Create an index only when the table exists and the index does not."""
    if index_name.lower() in _existing_indexes(table_name):
        return
    op.create_index(index_name, table_name, columns, unique=unique)


def upgrade() -> None:
    """Create missing core tables + indexes. Never drops anything."""
    _create_table_if_missing(
        "users",
        lambda: op.create_table(
            "users",
            sa.Column("id", sa.String(length=36), nullable=False),
            sa.Column("email", sa.String(length=255), nullable=False),
            sa.Column("username", sa.String(length=100), nullable=False),
            sa.Column("hashed_password", sa.String(length=255), nullable=False),
            sa.Column("full_name", sa.String(length=255), nullable=True),
            sa.Column("is_active", sa.Boolean(), nullable=False),
            sa.Column("is_verified", sa.Boolean(), nullable=False),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
            sa.PrimaryKeyConstraint("id"),
        ),
    )
    _create_index_if_missing("ix_users_email", "users", ["email"], unique=True)
    _create_index_if_missing("ix_users_username", "users", ["username"], unique=True)

    _create_table_if_missing(
        "images",
        lambda: op.create_table(
            "images",
            sa.Column("id", sa.String(length=36), nullable=False),
            sa.Column("request_id", sa.String(length=36), nullable=False),
            sa.Column("session_id", sa.String(length=36), nullable=True),
            sa.Column("user_id", sa.String(length=36), nullable=True),
            sa.Column("original_filename", sa.String(length=500), nullable=False),
            sa.Column("stored_filename", sa.String(length=500), nullable=False),
            sa.Column("file_path", sa.String(length=1000), nullable=False),
            sa.Column("file_size", sa.Integer(), nullable=False),
            sa.Column("mime_type", sa.String(length=100), nullable=False),
            sa.Column("image_hash", sa.String(length=64), nullable=True),
            sa.Column("image_url", sa.String(length=1000), nullable=False),
            sa.Column("width", sa.Integer(), nullable=True),
            sa.Column("height", sa.Integer(), nullable=True),
            sa.Column(
                "processing_status", sa.String(length=20), nullable=False,
                server_default=sa.text("'pending'"),
            ),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
            sa.ForeignKeyConstraint(["user_id"], ["users.id"]),
            sa.PrimaryKeyConstraint("id"),
        ),
    )
    _create_index_if_missing("ix_images_request_id", "images", ["request_id"], unique=True)
    _create_index_if_missing("ix_images_session_id", "images", ["session_id"], unique=False)
    _create_index_if_missing("ix_images_user_id", "images", ["user_id"], unique=False)

    _create_table_if_missing(
        "analyses",
        lambda: op.create_table(
            "analyses",
            sa.Column("id", sa.String(length=36), nullable=False),
            sa.Column("request_id", sa.String(length=36), nullable=False),
            sa.Column("image_id", sa.String(length=36), nullable=False),
            sa.Column("user_id", sa.String(length=36), nullable=True),
            sa.Column("status", sa.String(length=20), nullable=False),
            sa.Column("processing_time", sa.Float(), nullable=True),
            sa.Column("retry_count", sa.Integer(), nullable=False),
            sa.Column("version_number", sa.Integer(), nullable=False),
            sa.Column("material", sa.String(length=255), nullable=True),
            sa.Column("gold_purity", sa.String(length=100), nullable=True),
            sa.Column("weight", sa.Float(), nullable=True),
            sa.Column("category", sa.String(length=100), nullable=True),
            sa.Column("estimated_price", sa.Float(), nullable=True),
            sa.Column("confidence", sa.Float(), nullable=True),
            sa.Column("gemstones", sa.Text(), nullable=True),
            sa.Column("style", sa.String(length=100), nullable=True),
            sa.Column("era", sa.String(length=100), nullable=True),
            sa.Column("condition", sa.String(length=255), nullable=True),
            sa.Column("summary", sa.Text(), nullable=True),
            sa.Column("raw_response", sa.Text(), nullable=True),
            sa.Column("analyzed_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
            sa.ForeignKeyConstraint(["image_id"], ["images.id"]),
            sa.ForeignKeyConstraint(["request_id"], ["images.request_id"]),
            sa.ForeignKeyConstraint(["user_id"], ["users.id"]),
            sa.PrimaryKeyConstraint("id"),
        ),
    )
    _create_index_if_missing("ix_analyses_image_id", "analyses", ["image_id"], unique=False)
    _create_index_if_missing("ix_analyses_request_id", "analyses", ["request_id"], unique=False)
    _create_index_if_missing("ix_analyses_user_id", "analyses", ["user_id"], unique=False)

    _create_table_if_missing(
        "history",
        lambda: op.create_table(
            "history",
            sa.Column("id", sa.String(length=36), nullable=False),
            sa.Column("user_id", sa.String(length=36), nullable=True),
            sa.Column("analysis_id", sa.String(length=36), nullable=False),
            sa.Column("image_id", sa.String(length=36), nullable=False),
            sa.Column("product_name", sa.String(length=500), nullable=True),
            sa.Column("image_url", sa.String(length=1000), nullable=True),
            sa.Column("status", sa.String(length=20), nullable=False),
            sa.Column("estimated_price", sa.Float(), nullable=True),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.ForeignKeyConstraint(["analysis_id"], ["analyses.id"]),
            sa.ForeignKeyConstraint(["image_id"], ["images.id"]),
            sa.ForeignKeyConstraint(["user_id"], ["users.id"]),
            sa.PrimaryKeyConstraint("id"),
        ),
    )
    _create_index_if_missing("ix_history_analysis_id", "history", ["analysis_id"], unique=False)
    _create_index_if_missing("ix_history_user_id", "history", ["user_id"], unique=False)

    _create_table_if_missing(
        "reports",
        lambda: op.create_table(
            "reports",
            sa.Column("id", sa.String(length=36), nullable=False),
            sa.Column("analysis_id", sa.String(length=36), nullable=True),
            sa.Column("user_id", sa.String(length=36), nullable=True),
            sa.Column("report_type", sa.String(length=50), nullable=False),
            sa.Column("format", sa.String(length=10), nullable=False),
            sa.Column("filename", sa.String(length=500), nullable=False),
            sa.Column("file_path", sa.String(length=1000), nullable=False),
            sa.Column("file_size", sa.Integer(), nullable=True),
            sa.Column("page_count", sa.Integer(), nullable=True),
            sa.Column("include_charts", sa.Boolean(), nullable=False),
            sa.Column("generated_at", sa.DateTime(timezone=True), nullable=False),
            sa.ForeignKeyConstraint(["analysis_id"], ["analyses.id"], ondelete="SET NULL"),
            sa.ForeignKeyConstraint(["user_id"], ["users.id"]),
            sa.PrimaryKeyConstraint("id"),
        ),
    )
    _create_index_if_missing("ix_reports_analysis_id", "reports", ["analysis_id"], unique=False)
    _create_index_if_missing("ix_reports_user_id", "reports", ["user_id"], unique=False)

    _create_table_if_missing(
        "retry_history",
        lambda: op.create_table(
            "retry_history",
            sa.Column("id", sa.String(length=36), nullable=False),
            sa.Column("request_id", sa.String(length=36), nullable=False),
            sa.Column("analysis_id", sa.String(length=36), nullable=False),
            sa.Column("attempt_number", sa.Integer(), nullable=False),
            sa.Column("retry_time", sa.DateTime(timezone=True), nullable=False),
            sa.Column("status", sa.String(length=20), nullable=False),
            sa.Column("result_summary", sa.Text(), nullable=True),
            sa.Column("error_message", sa.Text(), nullable=True),
            sa.Column("processing_time", sa.Float(), nullable=True),
            sa.ForeignKeyConstraint(["analysis_id"], ["analyses.id"]),
            sa.ForeignKeyConstraint(["request_id"], ["images.request_id"]),
            sa.PrimaryKeyConstraint("id"),
        ),
    )
    _create_index_if_missing("ix_retry_history_analysis_id", "retry_history", ["analysis_id"], unique=False)
    _create_index_if_missing("ix_retry_history_request_id", "retry_history", ["request_id"], unique=False)

    _create_table_if_missing(
        "version_history",
        lambda: op.create_table(
            "version_history",
            sa.Column("id", sa.String(length=36), nullable=False),
            sa.Column("request_id", sa.String(length=36), nullable=False),
            sa.Column("analysis_id", sa.String(length=36), nullable=True),
            sa.Column("version_number", sa.Integer(), nullable=False),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("result_json", sa.Text(), nullable=True),
            sa.Column("summary", sa.Text(), nullable=True),
            sa.ForeignKeyConstraint(["analysis_id"], ["analyses.id"]),
            sa.ForeignKeyConstraint(["request_id"], ["images.request_id"]),
            sa.PrimaryKeyConstraint("id"),
        ),
    )
    _create_index_if_missing("ix_version_history_analysis_id", "version_history", ["analysis_id"], unique=False)
    _create_index_if_missing("ix_version_history_request_id", "version_history", ["request_id"], unique=False)

    _create_table_if_missing(
        "processing_logs",
        lambda: op.create_table(
            "processing_logs",
            sa.Column("id", sa.String(length=36), nullable=False),
            sa.Column("request_id", sa.String(length=36), nullable=False),
            sa.Column("processing_step", sa.String(length=100), nullable=False),
            sa.Column("status", sa.String(length=20), nullable=False),
            sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("processing_time", sa.Float(), nullable=True),
            sa.Column("error_message", sa.Text(), nullable=True),
            sa.Column("metadata_json", sa.Text(), nullable=True),
            sa.ForeignKeyConstraint(["request_id"], ["images.request_id"]),
            sa.PrimaryKeyConstraint("id"),
        ),
    )
    _create_index_if_missing("ix_processing_logs_request_id", "processing_logs", ["request_id"], unique=False)
    _create_index_if_missing("ix_processing_logs_processing_step", "processing_logs", ["processing_step"], unique=False)

    _create_table_if_missing(
        "tool_execution_logs",
        lambda: op.create_table(
            "tool_execution_logs",
            sa.Column("id", sa.String(length=36), nullable=False),
            sa.Column("request_id", sa.String(length=36), nullable=False),
            sa.Column("analysis_id", sa.String(length=36), nullable=True),
            sa.Column("tool_name", sa.String(length=100), nullable=False),
            sa.Column("execution_order", sa.Integer(), nullable=False),
            sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("execution_time", sa.Float(), nullable=True),
            sa.Column("status", sa.String(length=20), nullable=False),
            sa.Column("output_summary", sa.Text(), nullable=True),
            sa.Column("error_message", sa.Text(), nullable=True),
            sa.ForeignKeyConstraint(["analysis_id"], ["analyses.id"]),
            sa.ForeignKeyConstraint(["request_id"], ["images.request_id"]),
            sa.PrimaryKeyConstraint("id"),
        ),
    )
    _create_index_if_missing(
        "ix_tool_execution_logs_analysis_id", "tool_execution_logs", ["analysis_id"], unique=False
    )
    _create_index_if_missing(
        "ix_tool_execution_logs_request_id", "tool_execution_logs", ["request_id"], unique=False
    )
    _create_index_if_missing(
        "ix_tool_execution_logs_tool_name", "tool_execution_logs", ["tool_name"], unique=False
    )

    _create_table_if_missing(
        "whatsapp_ingestions",
        lambda: op.create_table(
            "whatsapp_ingestions",
            sa.Column("id", sa.String(length=36), nullable=False),
            sa.Column("request_id", sa.String(length=36), nullable=False),
            sa.Column("external_user_id", sa.String(length=100), nullable=False),
            sa.Column("external_message_id", sa.String(length=100), nullable=False),
            sa.Column("external_media_id", sa.String(length=200), nullable=True),
            sa.Column("channel", sa.String(length=20), nullable=False),
            sa.Column("caption", sa.Text(), nullable=True),
            sa.Column("mime_type", sa.String(length=100), nullable=True),
            sa.Column("timestamp", sa.String(length=50), nullable=True),
            sa.Column("image_id", sa.String(length=36), nullable=True),
            sa.Column("file_size", sa.Integer(), nullable=True),
            sa.Column("status", sa.String(length=20), nullable=False),
            sa.Column("error_message", sa.Text(), nullable=True),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
            sa.PrimaryKeyConstraint("id"),
        ),
    )
    _create_index_if_missing(
        "ix_whatsapp_ingestions_request_id", "whatsapp_ingestions", ["request_id"], unique=True
    )
    _create_index_if_missing(
        "ix_whatsapp_ingestions_external_user_id", "whatsapp_ingestions", ["external_user_id"], unique=False
    )
    _create_index_if_missing(
        "ix_whatsapp_ingestions_external_message_id", "whatsapp_ingestions", ["external_message_id"], unique=True
    )

    _create_table_if_missing(
        "customers",
        lambda: op.create_table(
            "customers",
            sa.Column("id", sa.String(length=36), nullable=False),
            sa.Column("whatsapp_id", sa.String(length=100), nullable=False),
            sa.Column("full_name", sa.String(length=255), nullable=False),
            sa.Column("business_name", sa.String(length=255), nullable=False),
            sa.Column("gst_number", sa.String(length=20), nullable=False),
            sa.Column("address", sa.Text(), nullable=False),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column(
                "wallet_balance", sa.Integer(), nullable=False, server_default=sa.text("0")
            ),
            sa.Column(
                "is_registered", sa.Boolean(), nullable=False, server_default=sa.true()
            ),
            sa.PrimaryKeyConstraint("id"),
        ),
    )
    _create_index_if_missing("ix_customers_whatsapp_id", "customers", ["whatsapp_id"], unique=True)

    _create_table_if_missing(
        "onboarding_sessions",
        lambda: op.create_table(
            "onboarding_sessions",
            sa.Column("id", sa.String(length=36), nullable=False),
            sa.Column("whatsapp_id", sa.String(length=100), nullable=False),
            sa.Column("state", sa.String(length=30), nullable=False),
            sa.Column("pending_data", sa.Text(), nullable=True),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=True),
            sa.PrimaryKeyConstraint("id"),
        ),
    )
    _create_index_if_missing(
        "ix_onboarding_sessions_whatsapp_id", "onboarding_sessions", ["whatsapp_id"], unique=True
    )

    _create_table_if_missing(
        "audit_logs",
        lambda: op.create_table(
            "audit_logs",
            sa.Column("id", sa.String(length=36), nullable=False),
            sa.Column("user_id", sa.String(length=36), nullable=True),
            sa.Column("session_id", sa.String(length=36), nullable=True),
            sa.Column("request_id", sa.String(length=36), nullable=True),
            sa.Column("action", sa.String(length=100), nullable=False),
            sa.Column("resource_type", sa.String(length=50), nullable=True),
            sa.Column("resource_id", sa.String(length=36), nullable=True),
            sa.Column("status", sa.String(length=20), nullable=False),
            sa.Column("ip_address", sa.String(length=45), nullable=True),
            sa.Column("user_agent", sa.String(length=500), nullable=True),
            sa.Column("details", sa.Text(), nullable=True),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.PrimaryKeyConstraint("id"),
        ),
    )
    _create_index_if_missing("ix_audit_logs_user_id", "audit_logs", ["user_id"], unique=False)
    _create_index_if_missing("ix_audit_logs_session_id", "audit_logs", ["session_id"], unique=False)
    _create_index_if_missing("ix_audit_logs_request_id", "audit_logs", ["request_id"], unique=False)
    _create_index_if_missing("ix_audit_logs_action", "audit_logs", ["action"], unique=False)


def downgrade() -> None:
    """Intentional no-op.

    This revision is a non-destructive baseline: it only creates objects
    that are missing, so there is nothing for a downgrade to undo that
    would be safe to remove (dropping core tables here would delete
    production data).  To reset the database, use ``alembic stamp base``
    and ``init_db()``/``create_all()`` or an explicit, reviewed SQL
    script instead.
    """
    # Intentionally left empty — see docstring.
