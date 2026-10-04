"""A customer's agreement to the data notice (PRIV-2, India's DPDP Act).

One row per (phone number, notice version): when they agreed. Stored by phone number, not customer id, because the
agreement is given before the customer record exists. A new notice version needs a new agreement.
"""

import uuid
from datetime import datetime, timezone

from sqlalchemy import DateTime, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


class ConsentRecord(Base):
    __tablename__ = "consent_records"
    __table_args__ = (UniqueConstraint("whatsapp_id", "version", name="uq_consent_whatsapp_version"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    whatsapp_id: Mapped[str] = mapped_column(String(100), nullable=False, index=True)
    version: Mapped[str] = mapped_column(String(20), nullable=False)
    accepted_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=lambda: datetime.now(timezone.utc)
    )

    def __repr__(self) -> str:
        return f"<ConsentRecord(version={self.version})>"
