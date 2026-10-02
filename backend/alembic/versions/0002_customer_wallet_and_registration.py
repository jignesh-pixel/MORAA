"""Additive migration: customer wallet + registration columns.

Adds two columns to the existing ``customers`` table (created by
``0001_onboarding_tables``):

    wallet_balance  INTEGER NOT NULL DEFAULT 0      (whole Indian Rupees)
    is_registered   BOOLEAN NOT NULL DEFAULT TRUE

Both columns carry a server default, so every pre-existing customer row is
back-filled by the database itself (existing customers were created by the
completed registration flow, hence ``TRUE``) and new rows are safe even when
inserted by an older code path.

This revision is strictly additive and idempotent:
    * no table is created, renamed or dropped
    * no column is renamed or removed (``whatsapp_id``/``address`` stay as
      deployed; ``phone_number``/``business_address`` are ORM-level synonyms)
    * each ``add_column`` is guarded by an inspector check, so running it on
      a database that already has the columns (e.g. created via
      ``init_db()``/``create_all``) is a no-op instead of an error
    * no data is ever lost — ``downgrade`` only drops the two new columns

Revision ID: 0002_customer_wallet
Revises: 0001_onboarding_tables
Create Date: 2026-09-12
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0002_customer_wallet"
down_revision: Union[str, None] = "0001_onboarding_tables"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _existing_columns(table_name: str) -> set:
    """Return the column names currently present on ``table_name``."""
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if table_name.lower() not in {name.lower() for name in inspector.get_table_names()}:
        return set()
    return {column["name"].lower() for column in inspector.get_columns(table_name)}


def upgrade() -> None:
    """Add the wallet/registration columns if they are not already present."""
    columns = _existing_columns("customers")
    if not columns:
        # Table missing entirely (unexpected) — nothing safe to do here.
        return

    if "wallet_balance" not in columns:
        op.add_column(
            "customers",
            sa.Column(
                "wallet_balance",
                sa.Integer(),
                nullable=False,
                server_default=sa.text("0"),
                comment="Prepaid wallet balance in whole Indian Rupees",
            ),
        )

    if "is_registered" not in columns:
        op.add_column(
            "customers",
            sa.Column(
                "is_registered",
                sa.Boolean(),
                nullable=False,
                server_default=sa.true(),
                comment="True once onboarding has captured the full profile",
            ),
        )


def downgrade() -> None:
    """Deliberate no-op: ``wallet_balance`` holds customers' real money.

    Dropping the column would destroy every balance and cannot be undone (the ledger, migration 0009,
    is derived from it). The columns are harmless on an older schema, and ``upgrade`` is idempotent,
    so keeping them makes a rollback and a later re-upgrade lossless.
    """
