"""Durable background jobs (Q-5): work that must not be lost when the process restarts.

A row is written in the same moment the work is decided (an ops message to forward, an invoice to send). A worker loop
claims due rows, runs them, and marks them done; a failure is retried with growing delays and, after the last attempt,
parked as ``dead`` and raised in the operations alerts. ``dedupe_key`` makes enqueueing the same work twice a no-op.
"""

from datetime import datetime, timezone

from sqlalchemy import DateTime, Index, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.types import JSON

from app.database import Base

PENDING = "pending"
RUNNING = "running"
DONE = "done"
DEAD = "dead"


def _now() -> datetime:
    return datetime.now(timezone.utc)


class OutboxJob(Base):
    __tablename__ = "outbox_jobs"
    __table_args__ = (Index("ix_outbox_jobs_due", "status", "next_attempt_at"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    kind: Mapped[str] = mapped_column(String(40), nullable=False)
    dedupe_key: Mapped[str] = mapped_column(String(120), nullable=False, unique=True)
    payload: Mapped[dict] = mapped_column(JSON, nullable=False)
    status: Mapped[str] = mapped_column(String(10), nullable=False, default=PENDING)
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    next_attempt_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=_now)
    last_error: Mapped[str] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=_now, onupdate=_now)

    def __repr__(self) -> str:
        return f"<OutboxJob({self.id} {self.kind} {self.status})>"
