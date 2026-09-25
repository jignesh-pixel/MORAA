"""WhatsApp Pay (native in-chat ``order_details``) recharge orders.

One row per ``order_details`` message sent to a customer. The row maps
Meta's ``reference_id`` back to the customer and the exact amount ordered,
so a payment-status webhook can be reconciled against it.

ADDITIVE model: no existing table is changed. The wallet itself is still
only credited through ``wallet_service.credit_wallet`` and the existing
money-once claim (``audit_logs`` action ``razorpay_payment_captured`` keyed
on the Razorpay payment id), so a payment can never be credited twice even
if both the Razorpay webhook and the Meta payment webhook report it.

Statuses:
    created          row written, message not yet accepted by Meta
    sent             order_details accepted by Meta
    dispatch_failed  Meta rejected the order_details message (link fallback sent)
    pending          Meta reported the payment as pending
    captured         payment confirmed via the Meta payment lookup API
    failed           Meta reported a failed transaction
    expired          order expired without a captured payment (sweep)
    amount_mismatch  lookup amount != ordered amount (never credited)
"""

import uuid
from datetime import datetime, timezone

from sqlalchemy import Boolean, DateTime, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


class WhatsAppPaymentOrder(Base):
    __tablename__ = "whatsapp_payment_orders"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    reference_id: Mapped[str] = mapped_column(
        String(35), unique=True, nullable=False, index=True,
        comment="Meta order_details reference_id (max 35 chars)",
    )
    whatsapp_id: Mapped[str] = mapped_column(
        String(100), nullable=False, index=True,
        comment="customers.whatsapp_id the order was issued to",
    )
    amount_rupees: Mapped[int] = mapped_column(Integer, nullable=False, comment="Wallet credit in whole rupees")
    total_paise: Mapped[int] = mapped_column(Integer, nullable=False, comment="order total_amount.value sent to Meta (subtotal + tax)")
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="created", index=True)
    configuration_name: Mapped[str] = mapped_column(String(60), nullable=True)
    pg_order_id: Mapped[str] = mapped_column(String(64), nullable=True, comment="transaction.id (Razorpay order id)")
    pg_payment_id: Mapped[str] = mapped_column(String(64), nullable=True, comment="transaction.pg_transaction_id (pay_...)")
    credited: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    fallback_sent: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=True)
    last_error: Mapped[str] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc)
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
    )

    def __repr__(self) -> str:
        return (
            f"<WhatsAppPaymentOrder(reference_id={self.reference_id}, "
            f"whatsapp_id={self.whatsapp_id}, amount={self.amount_rupees}, status={self.status})>"
        )
