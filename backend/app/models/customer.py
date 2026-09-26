"""Customer model — registered Moraa Studio (WhatsApp) customers.

Created by the WhatsApp onboarding flow (Scenario 1) in
``app/services/onboarding_service.py``. This is an ADDITIVE model: the
existing ``whatsapp_ingestions`` table and the image → style-selection →
generation pipeline are not affected by it.

The WhatsApp number is the primary lookup key for customer identification
and is unique, so a WhatsApp number can never produce duplicate customers.
The column is named ``whatsapp_id`` (the name already deployed in migration
0001) and is ALSO exposed as ``phone_number`` via an ORM synonym so callers
can use either name without a destructive column rename.

The wallet balance (Scenarios 2/3) is stored as whole Indian Rupees in an
integer column — never a float — so no rounding error can occur in money
arithmetic.
"""

import uuid
from datetime import datetime, timezone

from sqlalchemy import Boolean, DateTime, Integer, String, Text, false, text, true
from sqlalchemy.orm import Mapped, mapped_column, synonym

from app.database import Base


class Customer(Base):
    """A registered Moraa Studio customer identified by WhatsApp ID.

    Registration data is supplied by the customer over WhatsApp and parsed
    by the onboarding service; nothing is ever inferred or invented.
    """

    __tablename__ = "customers"

    id: Mapped[str] = mapped_column(
        String(36),
        primary_key=True,
        default=lambda: str(uuid.uuid4()),
    )
    whatsapp_id: Mapped[str] = mapped_column(
        String(100),
        unique=True,
        nullable=False,
        index=True,
        comment="WhatsApp sender phone number (e.g. '919876543210') — unique per customer",
    )
    full_name: Mapped[str] = mapped_column(
        String(255),
        nullable=False,
        comment="Customer name exactly as supplied over WhatsApp",
    )
    business_name: Mapped[str] = mapped_column(
        String(255),
        nullable=False,
        comment="Business name exactly as supplied over WhatsApp",
    )
    gst_number: Mapped[str] = mapped_column(
        String(20),
        nullable=False,
        comment="GST number normalized to uppercase (never guessed)",
    )
    address: Mapped[str] = mapped_column(
        Text,
        nullable=False,
        comment="Business address exactly as supplied over WhatsApp",
    )

    # ── Wallet / registration state (Scenarios 2, 3) ───────────────────
    wallet_balance: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        default=0,
        server_default=text("0"),
        comment="Prepaid wallet balance in whole Indian Rupees (never negative)",
    )
    is_registered: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        default=True,
        server_default=true(),
        comment="True once onboarding has captured the full profile",
    )
    is_gst_verified: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        default=False,
        server_default=false(),
        comment="True only after a live GSTIN lookup returned Active",
    )

    # ── Backward-compatible aliases ────────────────────────────────────
    # The requested Customer schema uses ``phone_number`` and
    # ``business_address``; the deployed columns are named ``whatsapp_id``
    # and ``address``. Synonyms expose the requested names (reads, writes and
    # ORM filters) without a destructive rename or data migration.
    phone_number = synonym("whatsapp_id")
    business_address = synonym("address")

    # ── Timestamps ─────────────────────────────────────────────────────
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc)
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
    )

    @property
    def balance_rupees(self) -> int:
        """Wallet balance as a non-negative integer number of rupees."""
        return max(int(self.wallet_balance or 0), 0)

    def affordable_image_count(self, image_count: int) -> int:
        """How many of ``image_count`` images this balance can pay for.

        Uses the configured per-image price and integer rupees only.
        """
        from app.config import settings as _settings

        if image_count <= 0:
            return 0

        price = max(int(_settings.WALLET_IMAGE_PRICE_RUPEES), 1)
        return min(self.balance_rupees // price, image_count)

    def __repr__(self) -> str:
        return (
            f"<Customer(id={self.id}, whatsapp_id={self.whatsapp_id}, "
            f"business_name={self.business_name}, "
            f"wallet_balance={self.wallet_balance})>"
        )
