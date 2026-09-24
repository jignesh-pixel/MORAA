"""WhatsApp ingestion model — tracks every incoming WhatsApp message.

Each incoming WhatsApp image message creates an Ingestion record that
links the external Meta identifiers (user ID, message ID, media ID)
to the internally stored GemVision request_id and file path.

This model is the single source of truth for the WhatsApp ingestion
lifecycle. Part 3 will read this record to connect ingestion → generation.
"""

import uuid
from datetime import datetime, timezone

from sqlalchemy import DateTime, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


# Product codes stored on each paid ingestion. NULL (legacy rows written
# before migration 0004) is treated as PRODUCT_PACK_1 everywhere.
PRODUCT_PACK_1 = "PACK_1"
PRODUCT_WHITE_BG = "WHITE_BG"


class WhatsAppIngestion(Base):
    """Records an incoming WhatsApp message and its internal processing state.

    Status lifecycle:
        received → downloaded → stored → [Part 3: processing → completed → delivered → failed]
    """

    __tablename__ = "whatsapp_ingestions"

    id: Mapped[str] = mapped_column(
        String(36),
        primary_key=True,
        default=lambda: str(uuid.uuid4()),
    )
    request_id: Mapped[str] = mapped_column(
        String(36),
        unique=True,
        nullable=False,
        index=True,
        default=lambda: str(uuid.uuid4()),
        comment="Internal GemVision request ID — links to Image.request_id for storage",
    )

    # ── External Meta identifiers ──────────────────────────────────────
    external_user_id: Mapped[str] = mapped_column(
        String(100),
        nullable=False,
        index=True,
        comment="WhatsApp sender phone number (e.g. '919876543210')",
    )
    external_message_id: Mapped[str] = mapped_column(
        String(100),
        unique=True,
        nullable=False,
        index=True,
        comment="WhatsApp message ID from Meta (used for idempotency)",
    )
    external_media_id: Mapped[str] = mapped_column(
        String(200),
        nullable=True,
        comment="Meta media ID for the uploaded image",
    )

    # ── Channel metadata ───────────────────────────────────────────────
    channel: Mapped[str] = mapped_column(
        String(20),
        nullable=False,
        default="whatsapp",
        comment="Source channel: whatsapp, instagram, facebook, etc.",
    )
    caption: Mapped[str] = mapped_column(
        Text,
        nullable=True,
        comment="Image caption text if provided by the user",
    )
    mime_type: Mapped[str] = mapped_column(
        String(100),
        nullable=True,
        comment="MIME type of the received image",
    )
    timestamp: Mapped[str] = mapped_column(
        String(50),
        nullable=True,
        comment="WhatsApp message timestamp from Meta",
    )

    # ── Internal storage reference ─────────────────────────────────────
    image_id: Mapped[str] = mapped_column(
        String(36),
        nullable=True,
        comment="GemVision Image record ID (set after storage via UploadService)",
    )
    file_size: Mapped[int] = mapped_column(
        Integer,
        nullable=True,
        comment="Downloaded file size in bytes",
    )

    # ── Processing state ───────────────────────────────────────────────
    status: Mapped[str] = mapped_column(
        String(20),
        nullable=False,
        default="received",
        comment="Lifecycle: received → downloaded → stored → [Part 3: processing → completed → delivered → failed]",
    )
    error_message: Mapped[str] = mapped_column(
        Text,
        nullable=True,
        comment="Error details if ingestion failed at any stage",
    )

    # ── Product purchased (migration 0004) ─────────────────────────────
    product_code: Mapped[str] = mapped_column(
        String(20),
        nullable=True,
        comment="PACK_1 | WHITE_BG. NULL = legacy row, treated as PACK_1",
    )
    amount_charged: Mapped[int] = mapped_column(
        Integer,
        nullable=True,
        comment="Rupees actually debited for this order; refunds use this. NULL = legacy",
    )

    # ── Timestamps ─────────────────────────────────────────────────────
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
            f"<WhatsAppIngestion(id={self.id}, request_id={self.request_id}, "
            f"status={self.status}, user={self.external_user_id})>"
        )
