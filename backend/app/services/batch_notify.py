"""One "your images are ready" WhatsApp message per batch (Phase 8).

Every image delivered to Drive calls ``schedule``: an outbox job runs after BATCH_NOTIFY_DELAY_SECONDS (at most one
per customer per window). When it runs and the customer still has orders in progress it waits another window;
when nothing is left in progress it sends ONE message with the day's folder link and the SKUs left, then records
that (audit ``batch_notified``) so the same images are never announced twice. Inside Meta's 24-hour window the
message is free-form text; outside it the approved utility template is used, and without one an alert is logged.
"""

from __future__ import annotations

import json
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Optional

from app.config import settings
from app.utils.executors import run_io
from app.utils.logger import logger, mask_phone

OUTBOX_KIND = "batch_notify"
NOTIFIED_ACTION = "batch_notified"
SERVICE_WINDOW = timedelta(hours=24)
# Orders untouched for longer than this are stuck (the recovery sweeps deal with them), not part of the batch.
IN_FLIGHT_WINDOW = timedelta(hours=4)


def _delay() -> int:
    return max(int(settings.BATCH_NOTIFY_DELAY_SECONDS or 0), 1)


def schedule(customer_id: str, windows_ahead: int = 0) -> None:
    """(blocking, never raises) Ask for a ready-check after the quiet time; repeat calls in one window are one job."""
    from app.services import outbox

    delay = _delay()
    bucket = int(time.time() // delay) + windows_ahead
    try:
        outbox.enqueue_job(OUTBOX_KIND, {"customer_id": customer_id}, f"notify:{customer_id}:{bucket}",
                           delay_seconds=delay * (windows_ahead + 1))
    except Exception as e:  # noqa: BLE001
        logger.error(f"Ready message could not be scheduled: {type(e).__name__}")


def _variants(phone: str) -> set:
    return {phone, f"+{phone}", phone[-10:]} if phone.isdigit() else {phone}


def _state(customer_id: str) -> Optional[Dict[str, Any]]:
    """(blocking) What the ready-check needs: orders in progress, Drive deliveries not yet announced, SKUs left."""
    from sqlalchemy import func

    from app.database import SessionLocal
    from app.models.audit_log import AuditLog
    from app.models.customer import Customer
    from app.models.whatsapp_ingestion import WhatsAppIngestion
    from app.services import sku_packs
    from app.services.drive_delivery import CHANNEL_DRIVE, STATUS_DRIVE_PENDING
    from app.services.meta_whatsapp_service import STUCK_PAID_STATUSES

    with SessionLocal() as db:
        customer = db.get(Customer, customer_id)
        if customer is None:
            return None
        phones = _variants(customer.whatsapp_id)
        mine = WhatsAppIngestion.external_user_id.in_(phones)
        in_flight = db.query(func.count(WhatsAppIngestion.id)).filter(
            mine, WhatsAppIngestion.status.in_(tuple(STUCK_PAID_STATUSES) + (STATUS_DRIVE_PENDING,)),
            WhatsAppIngestion.updated_at >= datetime.now(timezone.utc) - IN_FLIGHT_WINDOW,
        ).scalar() or 0
        last = db.query(func.max(AuditLog.created_at)).filter(
            AuditLog.action == NOTIFIED_ACTION, AuditLog.resource_id == customer_id
        ).scalar()
        delivered = db.query(WhatsAppIngestion).filter(
            mine, WhatsAppIngestion.status == "delivered", WhatsAppIngestion.delivery_channel == CHANNEL_DRIVE,
            *([WhatsAppIngestion.updated_at > last] if last is not None else []),
        )
        count = delivered.count()
        newest = delivered.order_by(WhatsAppIngestion.updated_at.desc()).first()
        last_inbound = db.query(func.max(WhatsAppIngestion.created_at)).filter(mine).scalar()
        return {
            "phone": customer.whatsapp_id, "in_flight": int(in_flight), "count": count,
            "day": newest.updated_at if newest is not None else None,
            "balance": sku_packs.balance(db, customer_id), "last_inbound": last_inbound,
            "name": customer.full_name or "", "email": customer.email or "",
        }


def _record(customer_id: str, count: int, channel: str) -> None:
    from app.database import SessionLocal
    from app.models.audit_log import AuditLog

    with SessionLocal() as db:
        db.add(AuditLog(action=NOTIFIED_ACTION, resource_type="customer", resource_id=customer_id, status="success",
                        details=json.dumps({"images": count, "channel": channel})))
        db.commit()


def _aware(value: Optional[datetime]) -> Optional[datetime]:
    return value.replace(tzinfo=timezone.utc) if value is not None and value.tzinfo is None else value


async def handle_outbox_job(payload: Dict[str, Any]) -> bool:
    """Send the ready message when the batch is complete. Raises when an in-window send fails (the outbox retries)."""
    from app.services import drive_layout, sku_messages
    from app.services.google_drive import folder_link
    from app.services.meta_whatsapp_service import send_whatsapp_template, send_whatsapp_text

    customer_id = payload["customer_id"]
    state = await run_io(_state, customer_id)
    if state is None or state["count"] == 0:
        return True
    if state["in_flight"]:
        await run_io(schedule, customer_id, 1)                 # still working: look again after the next quiet time
        return True
    folder_id, _label = await drive_layout.ensure_day_folder(customer_id, _aware(state["day"]))
    link = folder_link(folder_id)
    last_inbound = _aware(state["last_inbound"])
    if last_inbound is not None and datetime.now(timezone.utc) - last_inbound < SERVICE_WINDOW:
        text = sku_messages.ready_message(link, state["balance"], state.get("name", ""), state.get("email", ""))
        if not await send_whatsapp_text(state["phone"], text):
            raise RuntimeError("ready message not sent")
        channel = "text"
    elif (settings.BATCH_NOTIFY_TEMPLATE_NAME or "").strip():
        if not await send_whatsapp_template(state["phone"], settings.BATCH_NOTIFY_TEMPLATE_NAME.strip(),
                                            settings.BATCH_NOTIFY_TEMPLATE_LANG, [link, str(state["balance"])]):
            raise RuntimeError("ready template not sent")
        channel = "template"
    else:
        logger.error(f"ALERT ready message not sent to {mask_phone(state['phone'])}: outside the 24-hour window and "
                     "no BATCH_NOTIFY_TEMPLATE_NAME is approved")
        channel = "none"
    await run_io(_record, customer_id, state["count"], channel)
    return True
