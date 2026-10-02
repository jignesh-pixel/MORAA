"""Take or renew the lease for a periodic job (ARC-2). See ``app/models/scheduler_lease.py``.

``holds_lease`` answers "should THIS process run the job now?". If the database cannot answer (table missing, database
down) it answers True: a single process then behaves exactly as before, and every job it guards is idempotent anyway.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from app.utils.executors import run_io
from app.utils.logger import logger

HOLDER = uuid.uuid4().hex                  # one id per process


def _try_acquire(name: str, ttl_seconds: float) -> bool:
    from app.database import SessionLocal

    now = datetime.now(timezone.utc)
    expires = now + timedelta(seconds=ttl_seconds)
    with SessionLocal() as db:
        params = {"n": name, "h": HOLDER, "now": now, "exp": expires}
        # Renew our own lease, or take over an expired one: one guarded UPDATE, so two processes cannot both win.
        updated = db.execute(
            text("UPDATE scheduler_leases SET holder = :h, expires_at = :exp, updated_at = :now "
                 "WHERE name = :n AND (holder = :h OR expires_at < :now)"),
            params,
        ).rowcount
        if updated == 1:
            db.commit()
            return True
        try:
            db.execute(
                text("INSERT INTO scheduler_leases (name, holder, expires_at, updated_at) VALUES (:n, :h, :exp, :now)"),
                params,
            )
            db.commit()
            return True
        except IntegrityError:
            db.rollback()                      # the row exists and another live process holds it
            return False


def _release_all() -> None:
    from app.database import SessionLocal

    with SessionLocal() as db:
        db.execute(text("DELETE FROM scheduler_leases WHERE holder = :h"), {"h": HOLDER})
        db.commit()


async def release_leases() -> None:
    """Give up every lease this process holds (graceful shutdown), so a restarted process takes over at once instead
    of waiting for them to expire. Never raises."""
    try:
        await run_io(_release_all)
    except Exception as e:  # noqa: BLE001
        logger.warning(f"Could not release scheduler leases ({type(e).__name__}); they will expire on their own")


async def holds_lease(name: str, ttl_seconds: float) -> bool:
    try:
        return await run_io(_try_acquire, name, ttl_seconds)
    except Exception as e:  # noqa: BLE001 -- never stop the job because the lease table is unavailable
        logger.warning(f"Scheduler lease '{name}' unavailable ({type(e).__name__}); running the job here")
        return True
