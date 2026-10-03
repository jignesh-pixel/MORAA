"""Photo bursts become one bulk order (UX-1).

A customer who sends many photos in a row used to get a "choose Studio Shot or Catalog Pack" message for every photo.
Now, when a photo arrives within BULK_WINDOW_SECONDS of another waiting photo from the same number, its individual
buttons are held back; once the customer has been quiet for BULK_QUIET_SECONDS ONE message says
"12 photos, Clean Studio Shot, Rs 600: confirm?". Confirming charges every photo's price in one all-or-nothing step and
starts one Studio Shot per photo; the results come back on WhatsApp as each finishes. The single-photo flow is unchanged.

Only paying customers are grouped (team and trial customers keep the per-photo flow). The group prompt is scheduled
through the durable outbox, so a restart between the last photo and the prompt does not lose it; if the prompt cannot be
scheduled the photo simply gets its normal buttons.
"""

from __future__ import annotations

import asyncio
import uuid
from datetime import datetime, timedelta, timezone
from typing import Optional, Tuple

from sqlalchemy import update

from app.config import settings
from app.models.whatsapp_ingestion import WhatsAppIngestion
from app.utils.executors import run_io
from app.utils.logger import logger, mask_phone

BULK_OK = "gv_bulk_ok"
BULK_NO = "gv_bulk_no"
OUTBOX_KIND = "burst_prompt"


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _aware(value: datetime) -> datetime:
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


def bulk_button_id(button: str, group_id: str) -> str:
    return f"{button}:{group_id}"


def parse_bulk_button_id(button_id: str) -> Optional[Tuple[str, str]]:
    button, sep, group_id = (button_id or "").partition(":")
    if not sep or not group_id or button not in (BULK_OK, BULK_NO):
        return None
    return button, group_id


def is_enabled() -> bool:
    return bool(settings.BULK_ENABLED) and bool(settings.OUTBOX_ENABLED)


def is_burst(db, ingestion: WhatsAppIngestion) -> bool:
    """Is there another ungrouped photo from this number waiting for a choice, received within the burst window?"""
    since = _now() - timedelta(seconds=int(settings.BULK_WINDOW_SECONDS))
    return db.query(WhatsAppIngestion.id).filter(
        WhatsAppIngestion.external_user_id == ingestion.external_user_id,
        WhatsAppIngestion.status == "awaiting_choice",
        WhatsAppIngestion.group_id.is_(None),
        WhatsAppIngestion.id != ingestion.id,
        WhatsAppIngestion.created_at >= since,
    ).first() is not None


def prepare_group(sender: str) -> Optional[Tuple[str, int]]:
    """(blocking, own session) Gather this number's waiting photos into one group. None when there is nothing to
    prompt about: the customer is still sending, fewer than two photos wait, or another run already grouped them."""
    from app.database import SessionLocal

    with SessionLocal() as db:
        lookback = _now() - timedelta(minutes=int(settings.BULK_LOOKBACK_MINUTES))
        rows = (
            db.query(WhatsAppIngestion.id, WhatsAppIngestion.created_at)
            .filter(WhatsAppIngestion.external_user_id == sender, WhatsAppIngestion.status == "awaiting_choice",
                    WhatsAppIngestion.group_id.is_(None), WhatsAppIngestion.created_at >= lookback)
            .order_by(WhatsAppIngestion.created_at)
            .limit(int(settings.BULK_MAX_PHOTOS))
            .all()
        )
        if len(rows) < 2:
            return None
        newest = max(_aware(r[1]) for r in rows)
        if _now() - newest < timedelta(seconds=int(settings.BULK_QUIET_SECONDS) - 1):
            return None                               # still sending: the newest photo's own job will prompt later
        group_id = str(uuid.uuid4())
        claimed = db.execute(
            update(WhatsAppIngestion)
            .where(WhatsAppIngestion.id.in_([r[0] for r in rows]), WhatsAppIngestion.status == "awaiting_choice",
                   WhatsAppIngestion.group_id.is_(None))
            .values(group_id=group_id)
        ).rowcount
        db.commit()
        return (group_id, int(claimed)) if claimed >= 2 else None


def _balance_and_price(sender: str) -> Tuple[int, int]:
    from app.database import SessionLocal
    from app.services.wallet_service import find_customer_by_phone, get_balance

    with SessionLocal() as db:
        customer = find_customer_by_phone(db, sender)
        balance = get_balance(db, customer.whatsapp_id) if customer else 0
    return balance, max(int(settings.WHITE_BG_PRICE_RUPEES), 1)


async def send_group_prompt(sender: str) -> bool:
    """Send the one "N photos, Rs X: confirm?" message if this number's photos are ready to be grouped."""
    from app.services.meta_whatsapp_service import send_reply_buttons

    group = await run_io(prepare_group, sender)
    if group is None:
        return False
    group_id, count = group
    balance, price = await run_io(_balance_and_price, sender)
    total = count * price
    body = (
        f"You sent {count} photos. Clean Studio Shot (white background) for all {count} photos is "
        f"₹{total:,} (₹{price} each). Your wallet balance is ₹{balance:,}.\n"
        f"Tap Confirm to create all {count} images. Nothing is charged until you confirm."
    )
    sent = await send_reply_buttons(
        sender, body,
        [(bulk_button_id(BULK_OK, group_id), f"Confirm ₹{total:,}"), (bulk_button_id(BULK_NO, group_id), "Cancel")],
    )
    if not sent:
        logger.error(f"Bulk prompt not sent to {mask_phone(sender)} (group of {count})")
    return bool(sent)


async def schedule_prompt(sender: str, photo_id: str) -> bool:
    """Arrange the group prompt for after the customer goes quiet. True when it was scheduled."""
    from app.services import outbox

    quiet = max(int(settings.BULK_QUIET_SECONDS), 1)
    job_id = await run_io(outbox.enqueue_job, OUTBOX_KIND, {"sender": sender}, f"burst:{photo_id}", quiet + 30)
    if job_id is None:
        return False
    try:
        loop = asyncio.get_running_loop()
        loop.call_later(quiet, lambda: asyncio.ensure_future(outbox.run_job_now(job_id)))
    except RuntimeError:
        pass                                           # no running loop: the outbox sweep will run it
    return True


async def handle_outbox_job(payload: dict) -> bool:
    await send_group_prompt(payload["sender"])
    return True
