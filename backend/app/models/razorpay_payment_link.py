"""Razorpay recharge payment links we created (Q-3).

One row per link sent to a customer. The payment webhook credits a paid link; this row lets a periodic
sweep ask Razorpay about links that stay unpaid-looking, so a payment whose webhook never arrived is
still credited (once, through the same money-once claim as the webhook).

Statuses: created (not seen paid yet), paid (credited or already credited), expired (past the check window).
"""

import uuid
from datetime import datetime, timezone

from sqlalchemy import DateTime, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base

LINK_CREATED = "created"
LINK_PAID = "paid"
LINK_EXPIRED = "expired"


class RazorpayPaymentLink(Base):
    __tablename__ = "razorpay_payment_links"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    link_id: Mapped[str] = mapped_column(String(64), unique=True, nullable=False, comment="Razorpay plink_... id")
    whatsapp_id: Mapped[str] = mapped_column(String(100), nullable=False, index=True)
    amount_rupees: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default=LINK_CREATED, index=True)
    next_check_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=True, index=True)
    check_attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=lambda: datetime.now(timezone.utc)
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False,
        default=lambda: datetime.now(timezone.utc), onupdate=lambda: datetime.now(timezone.utc),
    )

    def __repr__(self) -> str:
        return f"<RazorpayPaymentLink({self.link_id} ₹{self.amount_rupees} {self.status})>"
