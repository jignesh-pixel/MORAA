"""Scheduler leases (ARC-2): which process runs a periodic job right now.

With several server processes every one starts the periodic jobs (payment reconcile, stuck-order recovery, alerts).
A lease row per job name says which process currently owns it; the owner renews it every pass, and when the owner dies
the lease expires and another process takes over.
"""

from datetime import datetime, timezone

from sqlalchemy import DateTime, String
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


class SchedulerLease(Base):
    __tablename__ = "scheduler_leases"

    name: Mapped[str] = mapped_column(String(60), primary_key=True)
    holder: Mapped[str] = mapped_column(String(64), nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=lambda: datetime.now(timezone.utc)
    )

    def __repr__(self) -> str:
        return f"<SchedulerLease({self.name} held by {self.holder})>"
