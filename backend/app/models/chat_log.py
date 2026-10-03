"""The WhatsApp chat dashboard's record (read-only view for the owner, partner and developer).

Every message that arrives from or is sent to a customer is written here as it happens (``ChatMessage``), every image the
bot produced is kept (``OrderOutput``), and every invoice sent is noted (``InvoiceRecord``). Together with the wallet ledger
and the orders table this lets one screen show a customer's whole story. Chats and files are kept for
RETENTION_CHAT_DAYS (90), like customer photos.
"""

import uuid
from datetime import datetime, timezone

from sqlalchemy import DateTime, Index, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


def _now() -> datetime:
    return datetime.now(timezone.utc)


class ChatMessage(Base):
    __tablename__ = "chat_messages"
    __table_args__ = (
        UniqueConstraint("direction", "wa_message_id", name="uq_chat_messages_direction_wa_id"),
        Index("ix_chat_messages_customer_time", "customer_phone", "created_at"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    customer_phone: Mapped[str] = mapped_column(String(100), nullable=False, comment="Normalised number of the customer")
    direction: Mapped[str] = mapped_column(String(3), nullable=False, comment="in = from the customer, out = from us")
    msg_type: Mapped[str] = mapped_column(String(30), nullable=False, comment="text, image, document, buttons, button_reply, flow, payment, other")
    text: Mapped[str] = mapped_column(Text, nullable=True)
    wa_message_id: Mapped[str] = mapped_column(String(200), nullable=True, comment="Meta message id (unique per direction)")
    ingestion_id: Mapped[str] = mapped_column(String(36), nullable=True, index=True)
    image_id: Mapped[str] = mapped_column(String(36), nullable=True, comment="images.id of a photo the customer sent")
    output_id: Mapped[str] = mapped_column(String(36), nullable=True, comment="order_outputs.id of an image we sent")
    meta_media_id: Mapped[str] = mapped_column(String(200), nullable=True)
    drive_file_id: Mapped[str] = mapped_column(String(200), nullable=True, comment="Google Drive copy of an inbound photo")
    drive_link: Mapped[str] = mapped_column(String(500), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=_now, index=True)

    def __repr__(self) -> str:
        return f"<ChatMessage({self.direction} {self.msg_type})>"


class OrderOutput(Base):
    __tablename__ = "order_outputs"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    ingestion_id: Mapped[str] = mapped_column(String(36), nullable=False, index=True)
    style: Mapped[str] = mapped_column(String(80), nullable=True)
    position: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    file_path: Mapped[str] = mapped_column(String(500), nullable=True, comment="Relative to the uploads folder; NULL once deleted")
    mime_type: Mapped[str] = mapped_column(String(50), nullable=False, default="image/png")
    size_bytes: Mapped[int] = mapped_column(Integer, nullable=True)
    meta_media_id: Mapped[str] = mapped_column(String(200), nullable=True, index=True)
    drive_file_id: Mapped[str] = mapped_column(String(200), nullable=True)
    drive_link: Mapped[str] = mapped_column(String(500), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=_now)

    def __repr__(self) -> str:
        return f"<OrderOutput({self.ingestion_id} #{self.position})>"


class InvoiceRecord(Base):
    __tablename__ = "invoice_records"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    payment_id: Mapped[str] = mapped_column(String(100), nullable=False, unique=True)
    customer_phone: Mapped[str] = mapped_column(String(100), nullable=False, index=True)
    amount_rupees: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[str] = mapped_column(String(10), nullable=False, default="pending", comment="pending, sent, failed")
    erpnext_invoice: Mapped[str] = mapped_column(String(100), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=_now, onupdate=_now)

    def __repr__(self) -> str:
        return f"<InvoiceRecord({self.payment_id} {self.status})>"
