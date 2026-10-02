"""Razorpay payments that were paid but could not be matched to a customer (MON-11).

A captured payment whose payer has no usable phone number (the Razorpay payload had none, or only a
Razorpay ``cust_...`` id) must not be credited to a made-up wallet that nobody can ever use. It is parked
here for a person to review; ``pending_payment_service.credit_pending_payment`` credits it to the right
customer later, through the same once-only money claim as every other payment.

Statuses: pending (waiting for a person), credited (given to a customer), dismissed (refunded or not ours).
"""

import uuid
from datetime import datetime, timezone

from sqlalchemy import DateTime, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base

STATUS_PENDING = "pending"
STATUS_CREDITED = "credited"
STATUS_DISMISSED = "dismissed"


class PendingPayment(Base):
    __tablename__ = "pending_payments"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    payment_id: Mapped[str] = mapped_column(String(64), unique=True, nullable=False, comment="Razorpay pay_... id")
    amount_rupees: Mapped[int] = mapped_column(Integer, nullable=False)
    currency: Mapped[str] = mapped_column(String(8), nullable=False, default="INR")
    event: Mapped[str] = mapped_column(String(40), nullable=True, comment="Razorpay event that reported it")
    payer_hint: Mapped[str] = mapped_column(String(100), nullable=True, comment="Whatever id Razorpay gave (e.g. cust_...)")
    reason: Mapped[str] = mapped_column(String(40), nullable=False, comment="missing_phone | not_a_phone_number")
    status: Mapped[str] = mapped_column(String(20), nullable=False, default=STATUS_PENDING, index=True)
    credited_whatsapp_id: Mapped[str] = mapped_column(String(100), nullable=True)
    note: Mapped[str] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=lambda: datetime.now(timezone.utc)
    )
    resolved_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=True)

    def __repr__(self) -> str:
        return f"<PendingPayment({self.payment_id} ₹{self.amount_rupees} {self.status})>"
