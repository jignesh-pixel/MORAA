"""Shared daily count of paid AI image calls (COST-1).

One row per UTC day. The count lives in the database, not in a process's memory, so every server process
shares ONE daily ceiling (``MAX_GENERATIONS_PER_DAY``) instead of each process having its own. Slots are taken
with a single guarded UPDATE (so two processes can never both take the last slot) and given back when the
call they were reserved for failed.
"""

from datetime import datetime, timezone

from sqlalchemy import CheckConstraint, DateTime, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


class GenerationSpend(Base):
    __tablename__ = "generation_spend"
    __table_args__ = (CheckConstraint("used >= 0", name="ck_generation_spend_used_nonneg"),)

    day: Mapped[str] = mapped_column(String(10), primary_key=True, comment="UTC date, YYYY-MM-DD")
    used: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False,
        default=lambda: datetime.now(timezone.utc), onupdate=lambda: datetime.now(timezone.utc),
    )

    def __repr__(self) -> str:
        return f"<GenerationSpend({self.day} used={self.used})>"
