"""Wallet ledger: append-only ``wallet_transactions`` plus a non-negative balance CHECK.

* Creates ``wallet_transactions`` (one signed row per balance change, see
  ``app/models/wallet_transaction.py``) with two partial unique indexes that make
  "one debit and one refund per order" and "one credit per payment id" database
  guarantees.
* Backfills one ``opening_balance`` row per customer whose balance is not zero, so that
  ``SUM(amount) == wallet_balance`` holds from the first moment.
* On PostgreSQL adds ``ck_customers_wallet_nonneg``. Refuses (with a clear message) to run
  if any customer already has a negative balance: fix that data by hand first.

Idempotent: every step is skipped if it already exists. The downgrade drops the CHECK and
the ledger table (the ledger is derived history; balances stay in ``customers``).

Revision ID: 0009_wallet_ledger
Revises: 0008_customer_access_tiers
Create Date: 2026-10-02
"""

import uuid
from datetime import datetime, timezone
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0009_wallet_ledger"
down_revision: Union[str, None] = "0008_customer_access_tiers"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_TABLE = "wallet_transactions"
_CHECK = "ck_customers_wallet_nonneg"


def _is_postgres() -> bool:
    return op.get_bind().dialect.name == "postgresql"


def _tables() -> set:
    return set(sa.inspect(op.get_bind()).get_table_names())


def upgrade() -> None:
    bind = op.get_bind()
    tables = _tables()
    if "customers" not in tables:
        return

    if _is_postgres():
        negative = bind.execute(sa.text("SELECT count(*) FROM customers WHERE wallet_balance < 0")).scalar()
        if negative:
            raise RuntimeError(
                f"{negative} customer(s) have a negative wallet_balance; correct them by hand before migrating."
            )

    if _TABLE not in tables:
        op.create_table(
            _TABLE,
            sa.Column("id", sa.String(36), primary_key=True),
            sa.Column("customer_id", sa.String(36), sa.ForeignKey("customers.id"), nullable=False),
            sa.Column("kind", sa.String(32), nullable=False),
            sa.Column("amount", sa.Integer(), nullable=False, comment="Signed whole rupees: credit +, debit -"),
            sa.Column("balance_after", sa.Integer(), nullable=False),
            sa.Column("ingestion_id", sa.String(36), nullable=True, comment="Order this row belongs to"),
            sa.Column("ref", sa.String(100), nullable=True, comment="External id, e.g. Razorpay payment id"),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.CheckConstraint("amount <> 0", name="ck_wallet_transactions_amount_nonzero"),
            sa.CheckConstraint("balance_after >= 0", name="ck_wallet_transactions_balance_nonneg"),
        )
        op.create_index("ix_wallet_transactions_customer_id", _TABLE, ["customer_id"])
        op.create_index(
            "uq_wallet_tx_ingestion_kind", _TABLE, ["ingestion_id", "kind"], unique=True,
            postgresql_where=sa.text("ingestion_id IS NOT NULL"), sqlite_where=sa.text("ingestion_id IS NOT NULL"),
        )
        op.create_index(
            "uq_wallet_tx_kind_ref", _TABLE, ["kind", "ref"], unique=True,
            postgresql_where=sa.text("ref IS NOT NULL"), sqlite_where=sa.text("ref IS NOT NULL"),
        )

    # Opening balances: only customers that have no ledger row yet and a non-zero balance.
    rows = bind.execute(sa.text(
        "SELECT c.id, c.wallet_balance FROM customers c "
        "WHERE c.wallet_balance <> 0 AND NOT EXISTS (SELECT 1 FROM wallet_transactions w WHERE w.customer_id = c.id)"
    )).fetchall()
    if rows:
        now = datetime.now(timezone.utc)
        ledger = sa.table(
            _TABLE,
            sa.column("id"), sa.column("customer_id"), sa.column("kind"), sa.column("amount"),
            sa.column("balance_after"), sa.column("created_at"),
        )
        op.bulk_insert(ledger, [
            {"id": str(uuid.uuid4()), "customer_id": r[0], "kind": "opening_balance",
             "amount": int(r[1]), "balance_after": int(r[1]), "created_at": now}
            for r in rows
        ])

    if _is_postgres():
        existing = {c["name"] for c in sa.inspect(bind).get_check_constraints("customers") if c.get("name")}
        if _CHECK not in existing:
            # NOT VALID then VALIDATE keeps the exclusive lock short; the data was verified non-negative above.
            op.execute(f"ALTER TABLE customers ADD CONSTRAINT {_CHECK} CHECK (wallet_balance >= 0) NOT VALID")
            op.execute(f"ALTER TABLE customers VALIDATE CONSTRAINT {_CHECK}")


def downgrade() -> None:
    tables = _tables()
    if "customers" in tables and _is_postgres():
        existing = {c["name"] for c in sa.inspect(op.get_bind()).get_check_constraints("customers") if c.get("name")}
        if _CHECK in existing:
            op.drop_constraint(_CHECK, "customers", type_="check")
    if _TABLE in tables:
        op.drop_table(_TABLE)
