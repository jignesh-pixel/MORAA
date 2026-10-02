"""Record AI provider calls and report their estimated cost (COST-4). See ``app/models/provider_call.py``.

``record`` never raises and never slows generation down: it is called from a worker thread after the call finished, and
if the table is missing (database not yet upgraded) or the database is down it backs off for a minute and stays quiet.
"""

from __future__ import annotations

import time
from datetime import datetime, timedelta, timezone
from typing import Dict, Optional

from sqlalchemy import func

from app.config import settings
from app.models.provider_call import ProviderCall
from app.utils.logger import logger

_RETRY_AFTER_SECONDS = 60.0
_disabled_until = 0.0
_warned = False


def reset_for_tests() -> None:
    global _disabled_until, _warned
    _disabled_until, _warned = 0.0, False


def error_kind(error: Optional[str]) -> Optional[str]:
    """A short category for a failure (never the message itself, which can carry request details)."""
    if not error:
        return None
    lowered = error.lower()
    if "timeout" in lowered or "timed out" in lowered:
        return "timeout"
    if any(m in lowered for m in ("per day", "daily", "billing", "insufficient_quota", "credit_balance")):
        return "quota"
    if any(m in lowered for m in ("429", "rate limit", "too many requests", "resource_exhausted")):
        return "rate_limit"
    if any(m in lowered for m in ("blocked", "safety", "400", "bad request", "invalid")):
        return "refused"
    return "other"


def cost_per_call_paise(provider: str) -> int:
    prices = {
        "gemini": settings.COST_PER_CALL_GEMINI_RUPEES,
        "openai": settings.COST_PER_CALL_OPENAI_RUPEES,
    }
    return max(int(round(float(prices.get((provider or "").lower(), 0.0) or 0.0) * 100)), 0)


def record(provider: str, model: Optional[str], success: bool, latency_seconds: float,
           error: Optional[str], request_id: Optional[str]) -> None:
    """Write one row (blocking; run it on the I/O pool). Never raises."""
    global _disabled_until, _warned
    if time.monotonic() < _disabled_until:
        return
    try:
        from app.database import SessionLocal

        with SessionLocal() as db:
            db.add(ProviderCall(
                provider=(provider or "unknown")[:30], model=(model or "")[:80] or None,
                outcome="success" if success else "failure", error_kind=None if success else error_kind(error),
                latency_ms=int(max(latency_seconds, 0.0) * 1000),
                est_cost_paise=cost_per_call_paise(provider) if success else 0,
                request_id=(request_id or "")[:64] or None,
            ))
            db.commit()
    except Exception as e:  # noqa: BLE001
        _disabled_until = time.monotonic() + _RETRY_AFTER_SECONDS
        if not _warned:
            _warned = True
            logger.warning(f"Provider call log unavailable ({type(e).__name__}); run `alembic upgrade head` to enable it")


def todays_summary(db, now: Optional[datetime] = None) -> Dict[str, int]:
    """Calls and estimated rupees spent since midnight UTC."""
    now = now or datetime.now(timezone.utc)
    start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    calls, paise = db.query(func.count(ProviderCall.id), func.coalesce(func.sum(ProviderCall.est_cost_paise), 0)) \
        .filter(ProviderCall.created_at >= start).one()
    return {"calls": int(calls), "rupees": int(paise) // 100}


def days_summary(db, days: int = 7) -> list:
    """Per-day, per-provider counts and estimated cost for the last ``days`` days (for the owner's review)."""
    since = datetime.now(timezone.utc) - timedelta(days=days)
    rows = db.query(ProviderCall).filter(ProviderCall.created_at >= since).all()
    out: Dict[tuple, Dict[str, int]] = {}
    for r in rows:
        key = (r.created_at.date().isoformat(), r.provider)
        bucket = out.setdefault(key, {"success": 0, "failure": 0, "paise": 0})
        bucket["success" if r.outcome == "success" else "failure"] += 1
        bucket["paise"] += int(r.est_cost_paise or 0)
    return [{"day": d, "provider": p, **v, "rupees": v["paise"] // 100} for (d, p), v in sorted(out.items())]
