"""Operations alerts to the owner's WhatsApp (OBS-4).

A background sweep checks a handful of "something needs a person" conditions on a schedule and sends ONE short
WhatsApp message per condition, then stays quiet for a cooldown (so a standing problem does not message every
five minutes). The cooldown is stored in ``audit_logs`` (action ``ops_alert_sent``), so it survives restarts and
is shared by every process. Alerts are always written to the log too, and counted in ``/metrics``.

Alerts contain counts and rupee amounts only: never a customer's name or phone number.
"""

import asyncio
import json
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import List

from sqlalchemy import func
from sqlalchemy.orm import Session

from app.config import settings
from app.models.audit_log import AuditLog
from app.services import metrics
from app.utils.executors import run_io
from app.utils.logger import logger

ALERT_AUDIT_ACTION = "ops_alert_sent"
# An order that took money but has not finished after this long needs a person.
STUCK_ORDER_AFTER = timedelta(minutes=20)
# Ignore the failure rate until there are at least this many finished orders (1 failure of 2 is not a trend).
MIN_ATTEMPTS_FOR_RATE_ALERT = 5


@dataclass(frozen=True)
class Alert:
    key: str
    message: str


def _numbers() -> List[str]:
    return [n.strip() for n in (settings.OPS_ALERT_WHATSAPP_NUMBERS or "").split(",") if n.strip()]


def evaluate_alerts(db: Session) -> List[Alert]:
    """Which conditions need attention right now? Pure reads; every check is isolated from the others."""
    alerts: List[Alert] = []

    def guarded(check) -> None:
        try:
            check()
        except Exception as e:  # noqa: BLE001 -- one broken check must not hide the rest
            db.rollback()
            logger.warning(f"Alert check {getattr(check, '__name__', 'check')} failed: {type(e).__name__}")

    def failure_rate() -> None:
        from app.services.generation_metrics import compute_generation_failure_rate

        report = compute_generation_failure_rate(db, window_hours=24)
        if report.exceeds_target and report.total_attempts >= MIN_ATTEMPTS_FOR_RATE_ALERT:
            alerts.append(Alert(
                "failure_rate",
                f"Moraa alert: {report.failure_rate:.0%} of the last {report.total_attempts} finished image orders "
                f"failed in 24 hours (limit 15%). Check the AI providers.",
            ))

    def spend() -> None:
        from app.ai import image_generation_manager as igm
        from app.services import spend_counter

        cap = settings.MAX_GENERATIONS_PER_DAY
        used = spend_counter.used(igm.current_spend_day())
        if used is None or not isinstance(cap, int) or cap <= 0:
            return
        if used >= cap * float(settings.OPS_ALERT_SPEND_WARN_FRACTION):
            alerts.append(Alert(
                "spend_near_cap",
                f"Moraa alert: {used} of today's {cap} image calls are used. New orders are declined at the limit.",
            ))

    def parked_payments() -> None:
        from app.models.pending_payment import STATUS_PENDING, PendingPayment

        count, total = db.query(func.count(PendingPayment.id), func.coalesce(func.sum(PendingPayment.amount_rupees), 0)) \
            .filter(PendingPayment.status == STATUS_PENDING).one()
        if count:
            alerts.append(Alert(
                "parked_payments",
                f"Moraa alert: {count} paid Razorpay payment(s), ₹{int(total):,} in total, are waiting for you to "
                f"credit them to a customer.",
            ))

    def review_rows() -> None:
        count = db.query(func.count(AuditLog.id)).filter(
            AuditLog.action.in_(("razorpay_clawback", "razorpay_payment_review")), AuditLog.status == "pending"
        ).scalar()
        if count:
            alerts.append(Alert(
                "payment_reviews",
                f"Moraa alert: {count} payment item(s) (a refund you could not fully take back, a dispute, or an "
                f"unusual payment) need a look.",
            ))

    def stuck_orders() -> None:
        from app.services.meta_whatsapp_service import STUCK_PAID_STATUSES
        from app.models.whatsapp_ingestion import WhatsAppIngestion

        cutoff = datetime.now(timezone.utc) - STUCK_ORDER_AFTER
        count = db.query(func.count(WhatsAppIngestion.id)).filter(
            WhatsAppIngestion.amount_charged > 0,
            WhatsAppIngestion.status.in_(STUCK_PAID_STATUSES),
            WhatsAppIngestion.updated_at < cutoff,
        ).scalar()
        if count:
            alerts.append(Alert(
                "stuck_orders",
                f"Moraa alert: {count} paid order(s) have been stuck for over 20 minutes. The recovery job will "
                f"refund them, but something is slowing orders down.",
            ))

    def daily_cost() -> None:
        limit = int(settings.OPS_ALERT_DAILY_COST_RUPEES or 0)
        if limit <= 0:
            return
        from app.services import provider_call_log

        today = provider_call_log.todays_summary(db)
        if today["rupees"] >= limit:
            alerts.append(Alert(
                "daily_cost",
                f"Moraa alert: today's estimated AI image spend is about Rs {today['rupees']:,} "
                f"({today['calls']} calls), past your Rs {limit:,} warning line.",
            ))

    def dead_jobs() -> None:
        from app.models.outbox_job import DEAD, OutboxJob

        count = db.query(func.count(OutboxJob.id)).filter(OutboxJob.status == DEAD).scalar()
        if count:
            alerts.append(Alert(
                "outbox_dead",
                f"Moraa alert: {count} background job(s) (an invoice or an ops-team message) gave up after repeated "
                f"failures and need a look.",
            ))

    for check in (failure_rate, spend, parked_payments, review_rows, stuck_orders, dead_jobs, daily_cost):
        guarded(check)
    return alerts


def _recently_sent(db: Session, key: str) -> bool:
    since = datetime.now(timezone.utc) - timedelta(minutes=int(settings.OPS_ALERT_COOLDOWN_MINUTES))
    return db.query(AuditLog.id).filter(
        AuditLog.action == ALERT_AUDIT_ACTION, AuditLog.resource_id == key, AuditLog.created_at >= since
    ).first() is not None


def _record_sent(db: Session, key: str, recipients: int) -> None:
    db.add(AuditLog(user_id=None, action=ALERT_AUDIT_ACTION, resource_id=key, resource_type="ops_alert",
                    status="success", details=json.dumps({"recipients": recipients})))
    db.commit()


async def dispatch_alerts(db: Session, alerts: List[Alert]) -> List[str]:
    """Send the alerts that are not in their cooldown. Returns the keys that were sent. Never raises."""
    sent: List[str] = []
    numbers = _numbers()
    for alert in alerts:
        try:
            if _recently_sent(db, alert.key):
                continue
            logger.bind(category="alert").warning(f"OPS_ALERT {alert.key}: {alert.message}")
            delivered = 0
            if numbers:
                from app.services.meta_whatsapp_service import send_whatsapp_text

                for number in numbers:
                    if await send_whatsapp_text(number, alert.message):
                        delivered += 1
            if numbers and not delivered:
                # Nothing got through: do not start the cooldown, try again next sweep.
                continue
            _record_sent(db, alert.key, delivered)
            metrics.registry.inc("moraa_alerts_sent_total", {"alert": alert.key})
            sent.append(alert.key)
        except Exception as e:  # noqa: BLE001
            db.rollback()
            logger.error(f"Alert {alert.key} could not be dispatched: {type(e).__name__}")
    return sent


def run_alert_pass() -> List[Alert]:
    """One evaluation (blocking; own session). Used by the sweep and by tests."""
    from app.database import SessionLocal

    with SessionLocal() as db:
        return evaluate_alerts(db)


async def run_alert_sweep_forever() -> None:
    """Background loop started from the application lifespan. Never raises except on cancel."""
    interval = int(settings.OPS_ALERT_SWEEP_INTERVAL_SECONDS or 0)
    if interval <= 0:
        logger.info("Operations alert sweep disabled (interval <= 0)")
        return
    interval = max(interval, 60)
    from app.database import SessionLocal

    while True:
        await asyncio.sleep(interval)
        try:
            from app.services.scheduler_lease import holds_lease

            if not await holds_lease("ops_alerts", interval * 2 + 30):
                continue                          # another process owns the alerts right now (ARC-2)
            alerts = await run_io(run_alert_pass)
            if alerts:
                with SessionLocal() as db:
                    await dispatch_alerts(db, alerts)
        except asyncio.CancelledError:
            raise
        except Exception as e:  # noqa: BLE001
            logger.error(f"Operations alert sweep failed: {type(e).__name__}: {e}")
