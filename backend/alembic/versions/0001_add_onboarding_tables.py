"""Additive migration: WhatsApp onboarding tables (Scenario 1).

Creates two NEW tables only:
    - customers            — registered WhatsApp customers
    - onboarding_sessions  — per-WhatsApp-ID registration state

This revision is strictly additive and idempotent:
    * no existing table is altered, renamed, or dropped
    * ``whatsapp_ingestions`` (and every other table) is untouched
    * each ``create_table`` is guarded by an inspector check so the
      migration is safe to run even when ``init_db()``'s
      ``Base.metadata.create_all()`` already created the tables in
      development (SQLite) environments

Revision ID: 0001_onboarding_tables
Revises:
Create Date: 2026-09-12
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0001_onboarding_tables"
down_revision: Union[str, None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _table_names() -> set:
    """Return the set of existing table names (case-insensitive)."""
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    return {name.lower() for name in inspector.get_table_names()}


def upgrade() -> None:
    """Create the additive onboarding tables if they do not already exist."""
    existing = _table_names()

    if "customers" not in existing:
        op.create_table(
            "customers",
            sa.Column("id", sa.String(length=36), nullable=False),
            sa.Column("whatsapp_id", sa.String(length=100), nullable=False),
            sa.Column("full_name", sa.String(length=255), nullable=False),
            sa.Column("business_name", sa.String(length=255), nullable=False),
            sa.Column("gst_number", sa.String(length=20), nullable=False),
            sa.Column("address", sa.Text(), nullable=False),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=True),
            sa.PrimaryKeyConstraint("id"),
        )
        op.create_index(
            "ix_customers_whatsapp_id",
            "customers",
            ["whatsapp_id"],
            unique=True,
        )

    if "onboarding_sessions" not in existing:
        op.create_table(
            "onboarding_sessions",
            sa.Column("id", sa.String(length=36), nullable=False),
            sa.Column("whatsapp_id", sa.String(length=100), nullable=False),
            sa.Column("state", sa.String(length=30), nullable=False),
            sa.Column("pending_data", sa.Text(), nullable=True),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=True),
            sa.PrimaryKeyConstraint("id"),
        )
        op.create_index(
            "ix_onboarding_sessions_whatsapp_id",
            "onboarding_sessions",
            ["whatsapp_id"],
            unique=True,
        )


def downgrade() -> None:
    """Deliberate no-op: these tables hold customer records and wallet balances.

    ``alembic downgrade base`` used to drop ``customers`` (every customer and their money) and
    ``onboarding_sessions``. That is unrecoverable data loss, so rolling back past this revision now
    keeps both tables. ``upgrade`` only creates what is missing, so a later re-upgrade is lossless.
    """
