"""Meta message ids the webhook has already handled (MON-10).

Meta re-delivers a webhook when it does not get a fast answer. Photos are already protected by the unique
``external_message_id`` on ``whatsapp_ingestions``; every other kind of message (text, button taps, form
replies) is protected by this table: the first delivery inserts the id, a repeat fails on the primary key
and is ignored. The database decides, so two simultaneous deliveries cannot both pass.
"""

from datetime import datetime, timezone

from sqlalchemy import DateTime, String
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


class ProcessedMessage(Base):
    __tablename__ = "processed_messages"

    message_id: Mapped[str] = mapped_column(String(255), primary_key=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=lambda: datetime.now(timezone.utc), index=True
    )

    def __repr__(self) -> str:
        return f"<ProcessedMessage({self.message_id})>"
