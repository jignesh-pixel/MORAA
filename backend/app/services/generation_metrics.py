"""Windowed generation failure-rate monitoring (P2·04).

Reuses the ``WhatsAppIngestion.status`` field already written by the
catalog-pack generation pipeline (``app/services/meta_whatsapp_service.py``)
— no new table, no external monitoring platform. A generation attempt is
counted once it reaches a terminal state; in-flight ("processing",
"pack_queued") and never-attempted (held for payment/registration, or
rejected by pre-validation before generation started) statuses are excluded
from both sides of the ratio, since nothing was actually generated for them.
"""

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Optional

from sqlalchemy import func
from sqlalchemy.orm import Session

from app.models.whatsapp_ingestion import WhatsAppIngestion
from app.utils.logger import logger

# Terminal statuses written by meta_whatsapp_service.py's catalog-pack flow.
SUCCESS_STATUSES = ("generated", "delivered")
FAILURE_STATUSES = ("failed", "delivery_failed")
TERMINAL_STATUSES = SUCCESS_STATUSES + FAILURE_STATUSES

# Target ceiling from the P2·04 punch-list item.
TARGET_FAILURE_RATE = 0.15


@dataclass
class FailureRateReport:
    window_hours: int
    total_attempts: int
    failed_attempts: int
    failure_rate: Optional[float]
    exceeds_target: bool


def compute_generation_failure_rate(
    db: Session, window_hours: int = 24
) -> FailureRateReport:
    """Failed / total completed generation attempts in the trailing window.

    ``failure_rate`` is None when there were no completed attempts in the
    window (nothing to divide by) rather than reporting a misleading 0%.
    """
    since = datetime.now(timezone.utc) - timedelta(hours=window_hours)

    rows = (
        db.query(WhatsAppIngestion.status, func.count(WhatsAppIngestion.id))
        .filter(
            WhatsAppIngestion.updated_at >= since,
            WhatsAppIngestion.status.in_(TERMINAL_STATUSES),
        )
        .group_by(WhatsAppIngestion.status)
        .all()
    )
    counts = {status: count for status, count in rows}
    total = sum(counts.values())
    failed = sum(counts.get(s, 0) for s in FAILURE_STATUSES)
    rate = (failed / total) if total else None

    return FailureRateReport(
        window_hours=window_hours,
        total_attempts=total,
        failed_attempts=failed,
        failure_rate=rate,
        exceeds_target=bool(rate is not None and rate > TARGET_FAILURE_RATE),
    )


def alert_if_failure_rate_exceeded(
    db: Session, window_hours: int = 24
) -> FailureRateReport:
    """Log a structured WARNING when the failure rate is above target.

    No external notification channel is configured, so the alert is a
    greppable log line (``GENERATION_FAILURE_RATE_ALERT``).
    """
    report = compute_generation_failure_rate(db, window_hours=window_hours)
    if report.exceeds_target:
        logger.warning(
            f"GENERATION_FAILURE_RATE_ALERT rate={report.failure_rate:.2%} "
            f"target={TARGET_FAILURE_RATE:.0%} failed={report.failed_attempts} "
            f"total={report.total_attempts} window_hours={window_hours}",
            extra={"category": "alert"},
        )
    return report
