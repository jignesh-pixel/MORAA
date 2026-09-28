"""Additive migration: customer access tiers (ADMIN / TRIAL / STANDARD).

Adds five NOT NULL columns to ``customers``, each with a server default so
existing rows are backfilled without a data migration:

* ``tier`` VARCHAR(32), default 'STANDARD' — access tier: ADMIN, TRIAL or
  STANDARD.
* ``trial_credits_total`` INTEGER, default 0 — complimentary orders granted
  by the owner (TRIAL tier).
* ``trial_credits_used`` INTEGER, default 0 — complimentary orders consumed.
* ``allowed_shots`` JSON (JSONB on PostgreSQL), default '["all"]' — shot /
  product types this customer may order.
* ``bypass_payment`` BOOLEAN, default false — skip wallet/payment checks.

Existing customers therefore become STANDARD with no trial credits, all
shots allowed and no payment bypass — i.e. behaviour is unchanged.

On PostgreSQL two CHECK constraints are also added (skipped if present):
``ck_customers_tier`` and ``ck_customers_trial_credits_nonneg``. They are
not added on other dialects (SQLite cannot ALTER TABLE ADD CONSTRAINT).

Idempotent: each column is only added when missing (e.g. not already created
by init_db()/create_all). downgrade drops the constraints (if present) and
then the columns that exist.

Revision ID: 0008_customer_access_tiers
Revises: 0007_customer_is_gst_verified
Create Date: 2026-09-28
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0008_customer_access_tiers"
down_revision: Union[str, None] = "0007_customer_is_gst_verified"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_TABLE = "customers"

_CHECKS = {
    "ck_customers_tier": "tier IN ('ADMIN','TRIAL','STANDARD')",
    "ck_customers_trial_credits_nonneg": "trial_credits_total >= 0 AND trial_credits_used >= 0",
}


def _is_postgres() -> bool:
    return op.get_bind().dialect.name == "postgresql"


def _columns() -> set:
    inspector = sa.inspect(op.get_bind())
    if _TABLE not in inspector.get_table_names():
        return set()
    return {c["name"] for c in inspector.get_columns(_TABLE)}


def _check_names() -> set:
    inspector = sa.inspect(op.get_bind())
    if _TABLE not in inspector.get_table_names():
        return set()
    return {c["name"] for c in inspector.get_check_constraints(_TABLE) if c.get("name")}


def _new_columns() -> list:
    if _is_postgres():
        shots_type = postgresql.JSONB()
        shots_default = sa.text("'[\"all\"]'::jsonb")
    else:
        shots_type = sa.JSON()
        shots_default = sa.text("'[\"all\"]'")
    return [
        sa.Column("tier", sa.String(32), nullable=False, server_default="STANDARD"),
        sa.Column("trial_credits_total", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("trial_credits_used", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("allowed_shots", shots_type, nullable=False, server_default=shots_default),
        sa.Column("bypass_payment", sa.Boolean(), nullable=False, server_default=sa.false()),
    ]


def upgrade() -> None:
    columns = _columns()
    if not columns:
        return
    for column in _new_columns():
        if column.name not in columns:
            op.add_column(_TABLE, column)

    if _is_postgres():
        existing = _check_names()
        for name, condition in _CHECKS.items():
            if name not in existing:
                op.create_check_constraint(name, _TABLE, condition)


def downgrade() -> None:
    columns = _columns()
    if not columns:
        return
    if _is_postgres():
        existing = _check_names()
        for name in _CHECKS:
            if name in existing:
                op.drop_constraint(name, _TABLE, type_="check")

    for name in ("bypass_payment", "allowed_shots", "trial_credits_used", "trial_credits_total", "tier"):
        if name in columns:
            op.drop_column(_TABLE, name)
