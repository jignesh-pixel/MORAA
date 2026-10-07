"""Central prices and the per-customer SKU credit ledger (migration 0022).

``price_settings`` holds one price per SKU, so prices live in one table instead of in code.

``customer_sku_credits`` is append-only, like ``wallet_transactions``: a pack purchase adds credits, an order uses
them, a refund gives them back. A customer's credits for a SKU are ``SUM(quantity)``. It is kept apart from
``customers.wallet_balance`` (whole rupees, CHECK >= 0), so credits never mix with money. The database refuses a
second row for the same ``(reference_id, action)``: a payment credits a pack once, an order uses credits once.
"""

import uuid
from datetime import datetime, timezone

from sqlalchemy import CheckConstraint, DateTime, ForeignKey, Integer, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


def _now() -> datetime:
    return datetime.now(timezone.utc)


class PriceSetting(Base):
    __tablename__ = "price_settings"
    __table_args__ = (CheckConstraint("price_rupees >= 0", name="ck_price_settings_price_nonneg"),)

    sku: Mapped[str] = mapped_column(String(64), primary_key=True)
    price_rupees: Mapped[int] = mapped_column(Integer, nullable=False, comment="Whole rupees")
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=_now, onupdate=_now)

    def __repr__(self) -> str:
        return f"<PriceSetting({self.sku} ₹{self.price_rupees})>"


class CustomerSkuCredit(Base):
    __tablename__ = "customer_sku_credits"
    __table_args__ = (
        CheckConstraint("quantity <> 0", name="ck_customer_sku_credits_quantity_nonzero"),
        UniqueConstraint("reference_id", "action", name="uq_customer_sku_credits_reference_action"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    customer_id: Mapped[str] = mapped_column(String(36), ForeignKey("customers.id"), nullable=False, index=True)
    sku: Mapped[str] = mapped_column(String(64), nullable=False)
    action: Mapped[str] = mapped_column(String(32), nullable=False, comment="purchase, consume, refund")
    quantity: Mapped[int] = mapped_column(Integer, nullable=False, comment="Signed credits: added +, used -")
    reference_id: Mapped[str] = mapped_column(String(64), nullable=False, comment="Payment id or order id")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=_now)

    def __repr__(self) -> str:
        return f"<CustomerSkuCredit({self.sku} {self.action} {self.quantity})>"
