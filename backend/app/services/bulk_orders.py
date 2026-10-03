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

from sqlalchemy import or_, update

from app.config import settings
from app.models.whatsapp_ingestion import WhatsAppIngestion
from app.utils.executors import run_io
from app.utils.logger import logger, mask_phone

BULK_OK = "gv_bulk_ok"
BULK_NO = "gv_bulk_no"
OUTBOX_KIND = "burst_prompt"
HELD = "pending"          # group_id of a photo whose own buttons were held back until the group prompt


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
        or_(WhatsAppIngestion.group_id.is_(None), WhatsAppIngestion.group_id == HELD),
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
                    or_(WhatsAppIngestion.group_id.is_(None), WhatsAppIngestion.group_id == HELD),
                    WhatsAppIngestion.created_at >= lookback)
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
                   or_(WhatsAppIngestion.group_id.is_(None), WhatsAppIngestion.group_id == HELD))
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


def release_held_photos(sender: str, group_id: Optional[str] = None) -> list:
    """(blocking, own session) Give held photos back their ordinary per-photo buttons. With ``group_id`` it undoes a
    group whose prompt could not be sent; without it, it frees photos still marked as held once the customer has gone
    quiet (for example the other photo of the burst was already chosen). Returns [(ingestion_id, message_id)] of the
    photos to send buttons for."""
    from app.database import SessionLocal

    with SessionLocal() as db:
        if group_id is None:
            lookback = _now() - timedelta(minutes=int(settings.BULK_LOOKBACK_MINUTES))
            waiting = db.query(WhatsAppIngestion.created_at).filter(
                WhatsAppIngestion.external_user_id == sender, WhatsAppIngestion.status == "awaiting_choice",
                or_(WhatsAppIngestion.group_id.is_(None), WhatsAppIngestion.group_id == HELD),
                WhatsAppIngestion.created_at >= lookback,
            ).all()
            if waiting and _now() - max(_aware(r[0]) for r in waiting) < timedelta(seconds=int(settings.BULK_QUIET_SECONDS) - 1):
                return []                                  # still sending
            marker = WhatsAppIngestion.group_id == HELD
        else:
            marker = WhatsAppIngestion.group_id == group_id
        rows = db.query(WhatsAppIngestion.id, WhatsAppIngestion.external_message_id).filter(
            WhatsAppIngestion.external_user_id == sender, WhatsAppIngestion.status == "awaiting_choice", marker,
        ).all()
        if not rows:
            return []
        db.execute(update(WhatsAppIngestion).where(WhatsAppIngestion.id.in_([r[0] for r in rows]), marker)
                   .values(group_id=None))
        db.commit()
        return [(r[0], r[1]) for r in rows]


async def send_single_buttons(sender: str, photos: list) -> None:
    """The ordinary product-choice buttons for each photo (the pre-bulk behaviour)."""
    from app.services.meta_whatsapp_service import send_product_selection_buttons
    from app.services.wallet_service import price_per_image

    balance, white_price = await run_io(_balance_and_price, sender)
    pack_price = await run_io(price_per_image)
    for ingestion_id, message_id in photos:
        await send_product_selection_buttons(
            recipient_id=sender, ingestion_id=ingestion_id, white_price=white_price, pack_price=pack_price,
            balance=balance, reply_to_message_id=message_id,
        )


async def send_group_prompt(sender: str) -> bool:
    """Send the one "N photos, Rs X: confirm?" message if this number's photos are ready to be grouped. A held photo
    is never left without buttons: if it ends up alone, or the prompt cannot be sent, it gets its ordinary buttons."""
    from app.services.meta_whatsapp_service import send_reply_buttons

    group = await run_io(prepare_group, sender)
    if group is None:
        freed = await run_io(release_held_photos, sender)
        if freed:
            await send_single_buttons(sender, freed)
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
        logger.error(f"Bulk prompt not sent to {mask_phone(sender)} (group of {count}); falling back to per-photo buttons")
        freed = await run_io(release_held_photos, sender, group_id)
        if freed:
            await send_single_buttons(sender, freed)
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
