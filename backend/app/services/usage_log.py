"""The customer's "Usage Logs" Google Sheet (Phase 8).

The sheet is a VIEW of ``customer_sku_credits`` and never the source of truth: one row per ledger row, oldest first
(Time (IST), Event, Images used, Balance left). ``sync`` appends only the rows the sheet does not have yet, in one
call; ``rebuild`` rewrites it from the ledger, which repairs anything a person changed in the sheet. Ledger changes
queue a ``usage_log_sync`` outbox job, coalesced per customer per minute.
"""

from __future__ import annotations

import time
from datetime import timezone
from typing import Any, Dict, List

from sqlalchemy.orm import Session

from app.models.sku_credit import (
    ACTION_CLAWBACK,
    ACTION_CONSUME,
    ACTION_EXPIRE,
    ACTION_PURCHASE,
    ACTION_REFUND,
    CustomerSkuCredit,
)
from app.models.customer import Customer
from app.services import drive_layout, google_drive
from app.services.google_drive import DriveError
from app.utils.executors import run_io
from app.utils.logger import logger

OUTBOX_KIND = "usage_log_sync"
_EVENTS = {
    ACTION_CONSUME: "Image created",
    ACTION_REFUND: "Credit returned (failed image)",
    ACTION_EXPIRE: "Credits expired",
    ACTION_CLAWBACK: "Credits taken back (refund)",
}
_IMAGES_USED = {ACTION_CONSUME: 1, ACTION_REFUND: -1}


def _event(row: CustomerSkuCredit) -> str:
    if row.action == ACTION_PURCHASE:
        return f"Pack purchased ({row.quantity} SKU{'' if row.quantity == 1 else 's'})"
    return _EVENTS.get(row.action, row.action)


def _ist(moment) -> str:
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return f"{moment.astimezone(drive_layout.IST):%d %b %Y %H:%M}"


def rows_for(db: Session, customer_id: str) -> List[List[Any]]:
    """The sheet's data rows for this customer, from the ledger (created_at, id order)."""
    rows = (db.query(CustomerSkuCredit).filter(CustomerSkuCredit.customer_id == customer_id)
            .order_by(CustomerSkuCredit.created_at, CustomerSkuCredit.id).all())
    registered = db.query(Customer.created_at).filter(Customer.id == customer_id).scalar()
    first = [[_ist(registered), "Account registered", 0, 0]] if registered is not None else []
    return first + [[_ist(r.created_at), _event(r), _IMAGES_USED.get(r.action, 0), int(r.balance_after)] for r in rows]


def _rows(customer_id: str) -> List[List[Any]]:
    from app.database import SessionLocal

    with SessionLocal() as db:
        return rows_for(db, customer_id)


async def sync(customer_id: str) -> int:
    """Append the ledger rows the sheet is missing, in one call. Returns how many data rows were added (0 when the
    sheet is already up to date, so running it twice is harmless)."""
    folders = await drive_layout.ensure_customer_folders(customer_id)
    async with drive_layout.customer_lock(customer_id):
        present = await google_drive.sheet_values(folders["sheet"], "A:A")
        rows = await run_io(_rows, customer_id)
        missing = rows[max(len(present) - 1, 0):]                 # the first sheet row is the header
        if not present:
            await google_drive.sheet_append(folders["sheet"], [drive_layout.USAGE_LOG_HEADER] + missing)
        elif missing:
            await google_drive.sheet_append(folders["sheet"], missing)
    return len(missing)


async def rebuild(customer_id: str) -> int:
    """Rewrite the sheet from the ledger: header plus every row. Returns the number of data rows."""
    folders = await drive_layout.ensure_customer_folders(customer_id)
    async with drive_layout.customer_lock(customer_id):
        rows = await run_io(_rows, customer_id)
        await google_drive.sheet_clear(folders["sheet"])
        await google_drive.sheet_append(folders["sheet"], [drive_layout.USAGE_LOG_HEADER] + rows)
    return len(rows)


def queue_sync(customer_id: str) -> None:
    """(blocking) Ask the outbox to bring the sheet up to date. One job per customer per minute, run just after that
    minute ends, so every ledger change of the minute is in one append. Never raises."""
    try:
        if not drive_layout.delivery_enabled():
            return
        from app.services import outbox

        now = time.time()
        bucket = int(now // 60)
        outbox.enqueue_job(OUTBOX_KIND, {"customer_id": customer_id}, f"usage:{customer_id}:{bucket}",
                           delay_seconds=int((bucket + 1) * 60 - now) + 1)
    except Exception as e:  # noqa: BLE001
        logger.error(f"Usage log sync could not be queued: {type(e).__name__}")


async def handle_outbox_job(payload: Dict[str, Any]) -> bool:
    """Outbox job "usage_log_sync". A retryable Drive failure raises (the outbox retries); anything else is done."""
    if not drive_layout.delivery_enabled():
        return True
    try:
        await sync(payload["customer_id"])
    except DriveError as e:
        if e.retryable:
            raise
        logger.error(f"Usage log sync gave up ({e.status} {e.reason})")
    return True
