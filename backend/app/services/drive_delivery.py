"""Deliver a finished white-background image to the customer's Google Drive folder (Phase 8).

The generation worker hands the image over with ``hand_over``: in ONE transaction the order moves
``generated -> drive_pending`` and a ``drive_deliver`` outbox job is recorded, so a crash cannot lose the image. The
job uploads it to ``{phone}/Images/{day}/NNN.png``, marks the order delivered and schedules the one "ready" message
(``batch_notify``). A job that fails is retried by the outbox (6 attempts); when it gives up, or Drive refuses the
upload for good, the image is sent on WhatsApp exactly as before. Only if that also fails is the order failed and
its credit (or wallet charge) given back.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, Optional

from app.services import metrics
from app.utils.executors import run_io
from app.utils.logger import logger

OUTBOX_KIND = "drive_deliver"
STATUS_DRIVE_PENDING = "drive_pending"
CHANNEL_DRIVE = "drive"
CHANNEL_WHATSAPP = "whatsapp"
FALLBACK_CAPTION = "Here's your Clean Studio Shot ✨"


def enabled() -> bool:
    from app.services import drive_layout

    return drive_layout.delivery_enabled()


BACKGROUND: "set" = set()           # uploads started by workers (strong references until done)


def customer_id_if_ready(phone: str) -> Optional[str]:
    """(blocking) The customer's id when they have a Drive folder shared with their email, else None."""
    from app.database import SessionLocal
    from app.services import drive_layout
    from app.services.wallet_service import find_customer_by_phone

    with SessionLocal() as db:
        customer = find_customer_by_phone(db, phone)
        customer_id = customer.id if customer else None
    return customer_id if customer_id and drive_layout.folder_shared_sync(customer_id) else None


def folder_ready(phone: str) -> bool:
    """(blocking) True when the customer behind this number has a Drive folder shared with their email."""
    return customer_id_if_ready(phone) is not None


def hand_over(ingestion_id: str, image_path: str, mime_type: str = "image/png") -> Optional[int]:
    """(blocking) Move a generated order to Drive delivery. Returns the outbox job id, or None when the order is not
    in ``generated`` any more or the job could not be recorded (the caller then delivers on WhatsApp as before)."""
    from sqlalchemy.exc import IntegrityError

    from app.database import SessionLocal
    from app.models.outbox_job import PENDING, OutboxJob
    from app.models.whatsapp_ingestion import WhatsAppIngestion

    try:
        with SessionLocal() as db:
            moved = db.query(WhatsAppIngestion).filter(
                WhatsAppIngestion.id == ingestion_id, WhatsAppIngestion.status == "generated"
            ).update({WhatsAppIngestion.status: STATUS_DRIVE_PENDING, WhatsAppIngestion.delivery_channel: CHANNEL_DRIVE},
                     synchronize_session=False)
            if moved != 1:
                db.rollback()
                return None
            job = OutboxJob(kind=OUTBOX_KIND, dedupe_key=f"drive:deliver:{ingestion_id}",
                            payload={"ingestion_id": ingestion_id, "path": str(image_path), "mime": mime_type},
                            status=PENDING, attempts=0, next_attempt_at=datetime.now(timezone.utc))
            db.add(job)
            db.commit()                      # status + job together
            return job.id
    except IntegrityError:
        return None                          # already handed over once (a re-run): deliver on WhatsApp instead
    except Exception as e:  # noqa: BLE001
        logger.error(f"Drive hand-over failed for {ingestion_id}: {type(e).__name__}: {e}")
        return None


def _load(ingestion_id: str) -> Optional[Dict[str, Any]]:
    """(blocking) The order, while it is still waiting for Drive delivery."""
    from app.database import SessionLocal
    from app.models.whatsapp_ingestion import WhatsAppIngestion
    from app.services.wallet_service import find_customer_by_phone

    with SessionLocal() as db:
        row = db.query(WhatsAppIngestion).filter(
            WhatsAppIngestion.id == ingestion_id, WhatsAppIngestion.status == STATUS_DRIVE_PENDING
        ).first()
        if row is None:
            return None
        from app.models.image import Image

        customer = find_customer_by_phone(db, row.external_user_id)
        image = db.get(Image, row.image_id) if row.image_id else None
        # Where the white-background worker keeps the finished image (used when the job payload is gone).
        kept = str(Path(image.file_path).parent / f"white_bg_{row.id}.png") if image else ""
        return {"customer_id": customer.id if customer else None, "recipient": row.external_user_id,
                "quote_id": row.external_message_id, "created_at": row.created_at, "path": kept}


def _finish(ingestion_id: str, channel: str, drive_file_id: Optional[str], expected: str = STATUS_DRIVE_PENDING) -> bool:
    """(blocking) ``expected`` -> delivered, once. False when something else (recovery) changed the order."""
    from app.database import SessionLocal
    from app.models.whatsapp_ingestion import WhatsAppIngestion

    values: Dict[Any, Any] = {WhatsAppIngestion.status: "delivered", WhatsAppIngestion.delivery_channel: channel,
                              WhatsAppIngestion.error_message: None}
    if drive_file_id:
        values[WhatsAppIngestion.drive_file_id] = drive_file_id
    with SessionLocal() as db:
        moved = db.query(WhatsAppIngestion).filter(
            WhatsAppIngestion.id == ingestion_id, WhatsAppIngestion.status == expected
        ).update(values, synchronize_session=False)
        db.commit()
        return moved == 1


async def _after_delivery(ingestion_id: str, customer_id: Optional[str]) -> None:
    """What the WhatsApp delivery path did after a delivery: trial metering. Plus the debounced ready message."""
    from app.database import SessionLocal
    from app.models.whatsapp_ingestion import WhatsAppIngestion
    from app.services import batch_notify
    from app.services import entitlement_service as ent

    db = SessionLocal()
    try:
        ingestion = db.get(WhatsAppIngestion, ingestion_id)
        await ent.record_trial_success(db, ingestion)             # trial orders only; never raises
    finally:
        db.close()
    if customer_id:
        await run_io(batch_notify.schedule, customer_id)


async def handle_outbox_job(payload: Dict[str, Any]) -> bool:
    """Upload one finished image to the customer's day folder. Raises on a retryable Drive failure (the outbox
    retries); a permanent refusal or a switched-off Drive goes straight to the WhatsApp fallback."""
    from app.services import drive_layout
    from app.services.google_drive import DriveError

    ingestion_id = payload["ingestion_id"]
    order = await run_io(_load, ingestion_id)
    if order is None:
        return True                                               # delivered, failed or refunded meanwhile
    if not enabled() or not order["customer_id"] or not Path(payload["path"]).is_file():
        return await _deliver_on_whatsapp(payload)
    try:
        uploaded = await drive_layout.upload_delivery_image(
            order["customer_id"], payload["path"], payload.get("mime") or "image/png", datetime.now(timezone.utc),
        )
    except DriveError as e:
        if e.retryable:
            raise
        logger.error(f"Drive refused the image for {ingestion_id} ({e.reason}); delivering on WhatsApp")
        return await _deliver_on_whatsapp(payload)
    if await run_io(_finish, ingestion_id, CHANNEL_DRIVE, uploaded.get("file_id")):
        metrics.registry.inc("moraa_drive_delivery_total", {"outcome": "drive"})
        await _after_delivery(ingestion_id, order["customer_id"])
    return True


def _claim_fallback(ingestion_id: str) -> bool:
    """(blocking) drive_pending -> generated with one guarded UPDATE: only one caller sends the WhatsApp copy."""
    from app.database import SessionLocal
    from app.models.whatsapp_ingestion import WhatsAppIngestion

    with SessionLocal() as db:
        moved = db.query(WhatsAppIngestion).filter(
            WhatsAppIngestion.id == ingestion_id, WhatsAppIngestion.status == STATUS_DRIVE_PENDING
        ).update({WhatsAppIngestion.status: "generated"}, synchronize_session=False)
        db.commit()
        return moved == 1


async def handle_dead_job(payload: Dict[str, Any]) -> bool:
    """The Drive upload failed on every attempt: send the image on WhatsApp instead."""
    logger.error(f"Drive delivery gave up for {payload.get('ingestion_id')}; delivering on WhatsApp")
    return await _deliver_on_whatsapp(payload)


async def _deliver_on_whatsapp(payload: Dict[str, Any]) -> bool:
    """The pre-Phase-8 delivery: upload to Meta and send the image. If that fails too the order is failed and its
    credit or charge goes back (exactly once, through the usual failure path)."""
    from app.database import SessionLocal
    from app.models.whatsapp_ingestion import WhatsAppIngestion
    from app.services import meta_whatsapp_service as mws

    ingestion_id = payload["ingestion_id"]
    order = await run_io(_load, ingestion_id)
    if order is None or not await run_io(_claim_fallback, ingestion_id):
        return True
    path = Path(payload.get("path") or order["path"] or "")
    data = await run_io(path.read_bytes) if path.is_file() else None
    media_id = await mws.upload_media_to_meta(data) if data else None
    del data
    sent = bool(media_id) and await mws.send_image_to_whatsapp(
        recipient_id=order["recipient"], media_id=media_id, caption=FALLBACK_CAPTION, reply_to_message_id=order["quote_id"],
    )
    if sent and await run_io(_finish, ingestion_id, CHANNEL_WHATSAPP, None, "generated"):
        metrics.registry.inc("moraa_drive_delivery_total", {"outcome": "whatsapp_fallback"})
        await _after_delivery(ingestion_id, None)
        return True
    if sent:
        return True
    metrics.registry.inc("moraa_drive_delivery_total", {"outcome": "failed"})
    db = SessionLocal()
    try:
        ingestion = db.query(WhatsAppIngestion).filter(
            WhatsAppIngestion.id == ingestion_id, WhatsAppIngestion.status == "generated"
        ).first()
        if ingestion is not None:
            mws._fail_delivery(db, ingestion, "Drive and WhatsApp delivery both failed")
            await mws._notify_failed_order(db, ingestion, "Clean Studio Shot")
    finally:
        db.close()
    return True


# A drive_pending order is moved on by its outbox job; one that has waited far longer than every retry together
# (6 attempts ≈ 1.7 h) lost its job and is delivered on WhatsApp by the recovery sweep.
DRIVE_PENDING_PATIENCE = timedelta(hours=3)


def _stalled(older_than: timedelta) -> list:
    """(blocking) Payloads of drive_pending orders older than ``older_than`` whose job is not pending or running."""
    from app.database import SessionLocal
    from app.models.outbox_job import PENDING, RUNNING, OutboxJob
    from app.models.whatsapp_ingestion import WhatsAppIngestion

    cutoff = datetime.now(timezone.utc) - older_than
    with SessionLocal() as db:
        rows = db.query(WhatsAppIngestion.id).filter(
            WhatsAppIngestion.status == STATUS_DRIVE_PENDING, WhatsAppIngestion.updated_at < cutoff
        ).limit(50).all()
        payloads = []
        for (ingestion_id,) in rows:
            job = db.query(OutboxJob).filter(OutboxJob.dedupe_key == f"drive:deliver:{ingestion_id}").first()
            if job is not None and job.status in (PENDING, RUNNING):
                continue
            payloads.append(dict(job.payload) if job is not None else {"ingestion_id": ingestion_id})
        return payloads


async def recover_stalled_deliveries(older_than: timedelta = DRIVE_PENDING_PATIENCE) -> int:
    """Recovery sweep step: deliver on WhatsApp every drive_pending order whose Drive job is gone. Never raises."""
    delivered = 0
    try:
        for payload in await run_io(_stalled, older_than):
            await _deliver_on_whatsapp(payload)
            delivered += 1
    except Exception as e:  # noqa: BLE001
        logger.error(f"Stalled Drive delivery recovery failed: {type(e).__name__}: {e}")
    return delivered
