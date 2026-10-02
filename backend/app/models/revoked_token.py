"""Refresh tokens that were used or revoked (SEC-4).

A refresh token is single-use: refreshing marks the old token's ``jti`` here and issues a new pair. Presenting a
token whose ``jti`` is already here means it was used twice, so either the owner or a thief holds a copy; the user's
older tokens are then all refused (``users.tokens_valid_after``). Rows can be deleted once the token would have expired.
"""

from datetime import datetime, timezone

from sqlalchemy import DateTime, String
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


class RevokedToken(Base):
    __tablename__ = "revoked_tokens"

    jti: Mapped[str] = mapped_column(String(64), primary_key=True)
    user_id: Mapped[str] = mapped_column(String(36), nullable=False, index=True)
    revoked_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=lambda: datetime.now(timezone.utc)
    )
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=True, index=True)

    def __repr__(self) -> str:
        return f"<RevokedToken({self.jti})>"
