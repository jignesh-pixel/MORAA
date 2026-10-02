"""Credit Razorpay recharge links whose payment webhook never arrived (Q-3).

Every link we send is recorded (``razorpay_payment_links``). This sweep asks Razorpay about links that are
still unpaid-looking, with growing waits between checks, and when Razorpay says one was paid it feeds the
payment through ``payment_routes.process_razorpay_event`` -- the same code, and the same once-only money
claim, as the webhook -- so a payment can never be credited twice whichever path sees it first.

Never logs the Razorpay key. Each link is handled in its own short database session.
"""

import asyncio
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Dict, Optional

import httpx
from fastapi import BackgroundTasks, HTTPException
from sqlalchemy import or_
from sqlalchemy.orm import Session

from app.config import settings
from app.models.razorpay_payment_link import LINK_CREATED, LINK_EXPIRED, LINK_PAID, RazorpayPaymentLink
from app.utils.logger import logger

RAZORPAY_LINKS_URL = "https://api.razorpay.com/v1/payment_links"
# Links older than this are no longer asked about (a customer paying a 3-day-old link is far rarer than a
# missed webhook, and the webhook normally covers it).
LINK_CHECK_WINDOW = timedelta(days=3)
# Give the normal webhook time to arrive before the sweep looks.
LINK_MIN_AGE = timedelta(minutes=2)
BACKOFF_BASE_SECONDS = 120
BACKOFF_CAP_SECONDS = 6 * 3600       # 2 min, 4, 8 ... up to 6 hours: about ten looks per link over three days
# Results of process_razorpay_event after which a link needs no further checks: the money is credited, belongs to
# the WhatsApp Pay path, or is parked in pending_payments / the review rows for a person. Anything else
# (credit_failed after repeated errors, unparseable, ignored) is looked at again after the backoff.
_TERMINAL_RESULTS = frozenset({
    "ok", "already_processed", "whatsapp_pay_order", "whatsapp_pay_order_review", "missing_phone",
    "unsupported_currency",
})
SWEEP_BATCH = 50
LOOKUP_TIMEOUT_SECONDS = 10.0


def next_link_check_delay(attempts: int) -> timedelta:
    return timedelta(seconds=min(BACKOFF_BASE_SECONDS * (2 ** min(max(attempts, 0), 12)), BACKOFF_CAP_SECONDS))


async def fetch_payment_link(link_id: str) -> Optional[Dict[str, Any]]:
    """GET one payment link from Razorpay. None on any failure (the caller retries later)."""
    if not settings.RAZORPAY_KEY_ID or not settings.RAZORPAY_KEY_SECRET:
        return None
    try:
        async with httpx.AsyncClient(
            timeout=LOOKUP_TIMEOUT_SECONDS,
            auth=httpx.BasicAuth(settings.RAZORPAY_KEY_ID, settings.RAZORPAY_KEY_SECRET),
        ) as client:
            resp = await client.get(f"{RAZORPAY_LINKS_URL}/{link_id}")
        if resp.status_code != 200:
            logger.error(f"Razorpay payment link lookup failed: status={resp.status_code}")
            return None
        data = resp.json()
    except Exception as e:
        logger.error(f"Razorpay payment link lookup error: {type(e).__name__}")
        return None
    return data if isinstance(data, dict) else None


def build_paid_events(link: Dict[str, Any]) -> list:
    """One ``payment_link.paid`` event per captured payment on the link, shaped like the real webhook."""
    events = []
    for payment in link.get("payments") or []:
        if not isinstance(payment, dict) or payment.get("status") != "captured" or not payment.get("payment_id"):
            continue
        events.append({
            "event": "payment_link.paid",
            "payload": {
                "payment": {"entity": {
                    "id": payment["payment_id"],
                    "amount": payment.get("amount", link.get("amount")),
                    "currency": payment.get("currency") or link.get("currency") or "",
                    "status": "captured",
                }},
                "payment_link": {"entity": link},
            },
        })
    return events


async def _check_link(db: Session, link_id_row: str, results: Dict[str, int]) -> None:
    """Check one link and schedule its next look. Never raises."""
    from app.api.routes.payment_routes import process_razorpay_event

    try:
        row = db.get(RazorpayPaymentLink, link_id_row)
        if row is None or row.status != LINK_CREATED:
            return
        link_id = row.link_id
        db.commit()                              # release the connection during the Razorpay call
        link = await fetch_payment_link(link_id)
        row = db.get(RazorpayPaymentLink, link_id_row)
        if row is None:
            return
        outcome = "lookup_failed"
        if link is not None:
            state = link.get("status")
            events = build_paid_events(link) if state in ("paid", "partially_paid") else []
            if events:
                outcome = "paid"
                try:
                    for event in events:
                        tasks = BackgroundTasks()
                        result = await process_razorpay_event(db, event, tasks)
                        if result.get("status") not in _TERMINAL_RESULTS:
                            outcome = "retry_later"      # not credited and not parked: look again after the backoff
                        try:
                            await tasks()                # receipt / invoice the webhook would have queued
                        except Exception as e:           # a failed receipt never undoes or blocks a credit
                            logger.error(f"Receipt for a reconciled Razorpay link failed: {type(e).__name__}: {e}")
                except HTTPException:
                    outcome = "retry_later"              # a retryable credit failure: look again after the backoff
                if outcome == "paid":
                    row = db.get(RazorpayPaymentLink, link_id_row)
                    row.status = LINK_PAID
                    db.commit()
            elif state in ("expired", "cancelled"):
                outcome = "closed"
                row.status = LINK_EXPIRED
                db.commit()
            else:
                outcome = "unpaid"
        results[outcome] = results.get(outcome, 0) + 1
        row = db.get(RazorpayPaymentLink, link_id_row)
        if row is not None and row.status == LINK_CREATED:
            row.check_attempts = int(row.check_attempts or 0) + 1
            row.next_check_at = datetime.now(timezone.utc) + next_link_check_delay(row.check_attempts)
            db.commit()
    except asyncio.CancelledError:
        raise
    except Exception as e:
        db.rollback()
        logger.error(f"Razorpay payment link reconcile of {link_id_row} failed: {type(e).__name__}: {e}")


async def reconcile_payment_links(session_factory: Callable[[], Session], limit: int = SWEEP_BATCH) -> Dict[str, int]:
    """One sweep. Returns how many links ended in each outcome."""
    now = datetime.now(timezone.utc)
    with session_factory() as db:
        db.query(RazorpayPaymentLink).filter(
            RazorpayPaymentLink.status == LINK_CREATED,
            RazorpayPaymentLink.created_at < now - LINK_CHECK_WINDOW,
        ).update({RazorpayPaymentLink.status: LINK_EXPIRED}, synchronize_session=False)
        db.commit()
        ids = [
            r[0]
            for r in db.query(RazorpayPaymentLink.id)
            .filter(
                RazorpayPaymentLink.status == LINK_CREATED,
                RazorpayPaymentLink.created_at <= now - LINK_MIN_AGE,
                or_(RazorpayPaymentLink.next_check_at.is_(None), RazorpayPaymentLink.next_check_at <= now),
            )
            .order_by(RazorpayPaymentLink.next_check_at.asc().nulls_first(), RazorpayPaymentLink.created_at)
            .limit(limit)
            .all()
        ]
    results: Dict[str, int] = {}
    for row_id in ids:
        with session_factory() as db:
            await _check_link(db, row_id, results)
    return results


async def run_payment_link_reconcile_forever() -> None:
    """Background loop started from the app lifespan. Idempotent, so a second instance is harmless."""
    interval = int(settings.RAZORPAY_LINK_RECONCILE_INTERVAL_SECONDS or 0)
    if interval <= 0:
        logger.info("Razorpay payment link reconcile sweep disabled (interval <= 0)")
        return
    interval = max(interval, 60)
    from app.database import SessionLocal

    while True:
        await asyncio.sleep(interval)
        if not settings.RAZORPAY_KEY_ID or not settings.RAZORPAY_KEY_SECRET:
            continue
        try:
            results = await reconcile_payment_links(SessionLocal)
            if results:
                logger.info(f"Razorpay payment link reconcile sweep: {results}")
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.error(f"Razorpay payment link reconcile sweep failed: {e}")
