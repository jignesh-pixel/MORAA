"""Central prices and the per-customer SKU credit ledger (migrations 0022, 0023).

``price_settings`` holds the current price per key (one row per key, e.g. the price of one SKU) with who set it and
from when; every change also writes an audit row (``price_changed``), which is the price history.

``customer_sku_credits`` is append-only, like ``wallet_transactions``: a pack purchase adds credits, an order uses
one, a failed order gives it back, unused credits expire, a refunded payment takes them back. A customer's credits
are ``SUM(quantity)``, which is also the newest row's ``balance_after``. It is kept apart from
``customers.wallet_balance`` (whole rupees), so credits never mix with money. The database refuses a second row for
the same ``(reference_id, action)`` (a payment grants once, an order consumes once and is refunded once) and a
negative ``balance_after``.
"""

import uuid
from datetime import datetime, timezone

from sqlalchemy import CheckConstraint, DateTime, ForeignKey, Integer, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base

# Ledger actions (reference_id in brackets).
ACTION_PURCHASE = "purchase"     # +units (Razorpay payment id)
ACTION_CONSUME = "consume"       # -1 (ingestion id)
ACTION_REFUND = "refund"         # +1 (ingestion id): the order failed, the credit comes back
ACTION_EXPIRE = "expire"         # -balance (expiry reference)
ACTION_CLAWBACK = "clawback"     # -n (Razorpay refund / dispute id): the payer got the money back

SKU_WHITE_BG = "white_bg"        # one white-background e-com shot
SKU_CREATIVE = "creative_pack"   # one Catalog Pack SKU: the 7-style photoshoot of one photo

PRICE_KEY_SKU = "sku_unit"       # price_settings key for the price of one SKU (GST included)


def _now() -> datetime:
    return datetime.now(timezone.utc)


class PriceSetting(Base):
    __tablename__ = "price_settings"
    __table_args__ = (CheckConstraint("price_rupees >= 0", name="ck_price_settings_price_nonneg"),)

    sku: Mapped[str] = mapped_column(String(64), primary_key=True, comment="Price key, e.g. sku_unit")
    price_rupees: Mapped[int] = mapped_column(Integer, nullable=False, comment="Whole rupees")
    effective_from: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=True)
    changed_by: Mapped[str] = mapped_column(String(100), nullable=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=_now, onupdate=_now)

    def __repr__(self) -> str:
        return f"<PriceSetting({self.sku} {self.price_rupees})>"


class CustomerSkuCredit(Base):
    __tablename__ = "customer_sku_credits"
    __table_args__ = (
        CheckConstraint("quantity <> 0", name="ck_customer_sku_credits_quantity_nonzero"),
        CheckConstraint("balance_after >= 0", name="ck_customer_sku_credits_balance_nonneg"),
        UniqueConstraint("reference_id", "action", name="uq_customer_sku_credits_reference_action"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    customer_id: Mapped[str] = mapped_column(String(36), ForeignKey("customers.id"), nullable=False, index=True)
    sku: Mapped[str] = mapped_column(String(64), nullable=False)
    action: Mapped[str] = mapped_column(String(32), nullable=False, comment="purchase, consume, refund, expire, clawback")
    quantity: Mapped[int] = mapped_column(Integer, nullable=False, comment="Signed credits: added +, used -")
    balance_after: Mapped[int] = mapped_column(Integer, nullable=False, comment="Customer's credits right after this row")
    reference_id: Mapped[str] = mapped_column(String(64), nullable=False, comment="Payment id, order id or expiry key")
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=True, comment="Purchase rows: valid until")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=_now)

    def __repr__(self) -> str:
        return f"<CustomerSkuCredit({self.sku} {self.action} {self.quantity} -> {self.balance_after})>"
