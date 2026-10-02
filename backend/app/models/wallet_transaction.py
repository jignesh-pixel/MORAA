"""Append-only wallet ledger.

Every change to ``customers.wallet_balance`` writes exactly one row here in the
SAME database transaction, so ``SUM(amount) == wallet_balance`` for every
customer at all times. That invariant is what lets a crash, a retry or a
duplicate webhook be audited: money can no longer move without a trace.

Sign convention: credits (payments, refunds, opening balance) are positive,
debits are negative. ``balance_after`` is the customer's balance right after
this row.

Idempotency is enforced by the database, not by application checks:
* one row per ``(ingestion_id, kind)`` -- an order is debited once and
  refunded once;
* one row per ``(kind, ref)`` -- a payment id credits once.
"""

import uuid
from datetime import datetime, timezone

from sqlalchemy import CheckConstraint, DateTime, ForeignKey, Index, Integer, String, text
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base

KIND_OPENING_BALANCE = "opening_balance"
KIND_DEBIT_ORDER = "debit_order"
KIND_REFUND_ORDER = "refund_order"
KIND_CREDIT_PAYMENT = "credit_payment"
KIND_CREDIT_WHATSAPP_PAY = "credit_whatsapp_pay"

LEDGER_KINDS = frozenset(
    {KIND_OPENING_BALANCE, KIND_DEBIT_ORDER, KIND_REFUND_ORDER, KIND_CREDIT_PAYMENT, KIND_CREDIT_WHATSAPP_PAY}
)


class WalletTransaction(Base):
    __tablename__ = "wallet_transactions"
    __table_args__ = (
        CheckConstraint("amount <> 0", name="ck_wallet_transactions_amount_nonzero"),
        CheckConstraint("balance_after >= 0", name="ck_wallet_transactions_balance_nonneg"),
        Index(
            "uq_wallet_tx_ingestion_kind",
            "ingestion_id",
            "kind",
            unique=True,
            postgresql_where=text("ingestion_id IS NOT NULL"),
            sqlite_where=text("ingestion_id IS NOT NULL"),
        ),
        Index(
            "uq_wallet_tx_kind_ref",
            "kind",
            "ref",
            unique=True,
            postgresql_where=text("ref IS NOT NULL"),
            sqlite_where=text("ref IS NOT NULL"),
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    customer_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("customers.id"), nullable=False, index=True
    )
    kind: Mapped[str] = mapped_column(String(32), nullable=False)
    amount: Mapped[int] = mapped_column(Integer, nullable=False, comment="Signed whole rupees: credit +, debit -")
    balance_after: Mapped[int] = mapped_column(Integer, nullable=False)
    ingestion_id: Mapped[str] = mapped_column(String(36), nullable=True, comment="Order this row belongs to")
    ref: Mapped[str] = mapped_column(String(100), nullable=True, comment="External id, e.g. Razorpay payment id")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=lambda: datetime.now(timezone.utc)
    )

    def __repr__(self) -> str:
        return f"<WalletTransaction({self.kind} {self.amount} -> {self.balance_after})>"
