"""One row per attempted AI image provider call (COST-4).

Lets the owner see what the AI actually cost per day and per provider, and be warned when a day runs past a
threshold. ``est_cost_paise`` is the owner-configured price per call (COST_PER_CALL_*_RUPEES), counted for successful
calls only (providers do not bill a refused or failed call); it is an estimate, not an invoice.
"""

from datetime import datetime, timezone

from sqlalchemy import DateTime, Index, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


class ProviderCall(Base):
    __tablename__ = "provider_calls"
    __table_args__ = (Index("ix_provider_calls_created", "created_at"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=lambda: datetime.now(timezone.utc)
    )
    provider: Mapped[str] = mapped_column(String(30), nullable=False)
    model: Mapped[str] = mapped_column(String(80), nullable=True)
    outcome: Mapped[str] = mapped_column(String(10), nullable=False)          # success | failure
    error_kind: Mapped[str] = mapped_column(String(30), nullable=True)        # timeout | rate_limit | quota | refused | other
    latency_ms: Mapped[int] = mapped_column(Integer, nullable=True)
    est_cost_paise: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    request_id: Mapped[str] = mapped_column(String(64), nullable=True)

    def __repr__(self) -> str:
        return f"<ProviderCall({self.provider} {self.outcome})>"
