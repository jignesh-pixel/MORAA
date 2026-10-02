"""Database-backed daily spend counter (COST-1). See ``app/models/generation_spend.py``.

Every function opens its own short session and returns ``None`` when the database cannot answer (table missing,
database down, SQLite without the table in tests). The caller then falls back to the in-process counter, so a
counter problem can never stop paying customers from being served. After a failure the shared counter is left
alone for ``RETRY_AFTER_SECONDS`` instead of failing again on every call.
"""

import time
from datetime import datetime, timezone
from typing import Optional

from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from app.utils.logger import logger

RETRY_AFTER_SECONDS = 60.0
_disabled_until = 0.0
_warned = False


def _session():
    from app.database import SessionLocal

    return SessionLocal()


def _usable() -> bool:
    return time.monotonic() >= _disabled_until


def _fail(exc: Exception) -> None:
    """Remember that the shared counter is unavailable for a while (and say so once)."""
    global _disabled_until, _warned
    _disabled_until = time.monotonic() + RETRY_AFTER_SECONDS
    if not _warned:
        _warned = True
        logger.warning(
            f"Shared spend counter unavailable ({type(exc).__name__}); using the in-process counter. "
            "Run `alembic upgrade head` so the daily ceiling is shared by all processes."
        )


def reset_for_tests() -> None:
    global _disabled_until, _warned
    _disabled_until, _warned = 0.0, False


def _ensure_row(db, day: str) -> None:
    now = datetime.now(timezone.utc)
    try:
        db.execute(
            text("INSERT INTO generation_spend (day, used, updated_at) VALUES (:d, 0, :n)"), {"d": day, "n": now}
        )
        db.commit()
    except IntegrityError:
        db.rollback()          # another process created today's row first: fine


def reserve(day: str, count: int, cap: Optional[int]) -> Optional[bool]:
    """Take ``count`` slots for ``day`` if they fit under ``cap``. True = taken, False = no room, None = unknown.

    One guarded UPDATE decides, so concurrent processes cannot both take the last slot. ``cap=None`` means no
    ceiling (the call is only counted).
    """
    if not _usable():
        return None
    count = max(int(count), 1)
    try:
        with _session() as db:
            for _ in range(2):
                query = "UPDATE generation_spend SET used = used + :n, updated_at = :now WHERE day = :d"
                params = {"n": count, "d": day, "now": datetime.now(timezone.utc)}
                if cap is not None:
                    query += " AND used + :n <= :cap"
                    params["cap"] = max(int(cap), 1)
                if db.execute(text(query), params).rowcount == 1:
                    db.commit()
                    return True
                db.rollback()
                exists = db.execute(text("SELECT 1 FROM generation_spend WHERE day = :d"), {"d": day}).first()
                if exists is not None:
                    return False            # row exists, so the guard (no room) refused it
                _ensure_row(db, day)        # first call of the day: create the row and try once more
            return False
    except Exception as e:  # noqa: BLE001 -- see module docstring
        _fail(e)
        return None


def release(day: str, count: int) -> Optional[bool]:
    """Give ``count`` slots back (the calls they were reserved for failed). Never goes below zero."""
    if not _usable():
        return None
    try:
        with _session() as db:
            db.execute(
                text("UPDATE generation_spend SET used = CASE WHEN used >= :n THEN used - :n ELSE 0 END, "
                     "updated_at = :now WHERE day = :d"),
                {"n": max(int(count), 0), "d": day, "now": datetime.now(timezone.utc)},
            )
            db.commit()
        return True
    except Exception as e:  # noqa: BLE001
        _fail(e)
        return None


def used(day: str) -> Optional[int]:
    """Calls counted so far for ``day`` (None when the shared counter cannot answer)."""
    if not _usable():
        return None
    try:
        with _session() as db:
            row = db.execute(text("SELECT used FROM generation_spend WHERE day = :d"), {"d": day}).first()
        return int(row[0]) if row else 0
    except Exception as e:  # noqa: BLE001
        _fail(e)
        return None
