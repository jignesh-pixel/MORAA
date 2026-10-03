"""Writes the WhatsApp chat dashboard's record as things happen. See ``app/models/chat_log.py``.

Everything here is best effort and never raises into the order or message flow: a missing table (database not yet
upgraded) or a database hiccup is logged once and the writer backs off for a minute. Writes run on the I/O pool and are
never waited for, so a slow database cannot slow a customer's chat.
"""

from __future__ import annotations

import asyncio
import time
import uuid
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

from sqlalchemy.exc import IntegrityError

from app.config import settings
from app.models.chat_log import ChatMessage, InvoiceRecord, OrderOutput
from app.utils.executors import run_io
from app.utils.logger import logger
from app.utils.phone import normalize_phone

_RETRY_AFTER_SECONDS = 60.0
_disabled_until = 0.0
_warned = False
_tasks: "set[asyncio.Task]" = set()


def reset_for_tests() -> None:
    global _disabled_until, _warned
    _disabled_until, _warned = 0.0, False


def enabled() -> bool:
    return bool(settings.CHAT_LOG_ENABLED) and time.monotonic() >= _disabled_until


def _fail(exc: Exception) -> None:
    global _disabled_until, _warned
    _disabled_until = time.monotonic() + _RETRY_AFTER_SECONDS
    if not _warned:
        _warned = True
        logger.warning(f"Chat log unavailable ({type(exc).__name__}); run `alembic upgrade head` to enable the dashboard record")


def _session():
    from app.database import SessionLocal

    return SessionLocal()


def fire(fn, *args: Any) -> None:
    """Run a blocking writer on the I/O pool without waiting for it (and without keeping its arguments alive longer
    than needed). Does nothing outside a running event loop's reach."""
    if not enabled():
        return

    async def _go() -> None:
        try:
            await run_io(fn, *args)
        except Exception:  # noqa: BLE001
            pass

    try:
        task = asyncio.ensure_future(_go())
    except RuntimeError:
        return
    _tasks.add(task)
    task.add_done_callback(_tasks.discard)


# ── describing what was sent ─────────────────────────────────────────────────────────────────────────────

def describe_payload(payload: Dict[str, Any]) -> Tuple[str, str, Optional[str]]:
    """(msg_type, readable text, meta media id) for a Meta send payload."""
    kind = payload.get("type", "other")
    if kind == "text":
        return "text", str((payload.get("text") or {}).get("body", "")), None
    if kind == "image":
        image = payload.get("image") or {}
        return "image", str(image.get("caption", "") or ""), image.get("id")
    if kind == "document":
        doc = payload.get("document") or {}
        label = str(doc.get("filename", "") or "")
        caption = str(doc.get("caption", "") or "")
        return "document", (f"📄 {label}\n{caption}".strip() if label else caption), doc.get("id")
    if kind == "interactive":
        interactive = payload.get("interactive") or {}
        itype = interactive.get("type")
        body = str((interactive.get("body") or {}).get("text", ""))
        if itype == "button":
            titles = [b.get("reply", {}).get("title", "") for b in (interactive.get("action") or {}).get("buttons", [])]
            return "buttons", f"{body}\n[ " + " | ".join(t for t in titles if t) + " ]", None
        if itype == "order_details":
            return "payment", body or "Payment request", None
        if itype == "flow":
            return "flow", body or "Form", None
        return "buttons", body, None
    return "other", f"[{kind}]", None


def _output_by_media(db, media_id: Optional[str]) -> Optional[OrderOutput]:
    if not media_id:
        return None
    return db.query(OrderOutput).filter(OrderOutput.meta_media_id == media_id).order_by(OrderOutput.created_at.desc()).first()


# ── writers (blocking; call through ``fire``) ────────────────────────────────────────────────────────────────

def write_out(payload: Dict[str, Any], wa_message_id: Optional[str]) -> None:
    to = normalize_phone(payload.get("to"))
    if not to:
        return
    msg_type, text, media_id = describe_payload(payload)
    try:
        with _session() as db:
            output = _output_by_media(db, media_id) if msg_type == "image" else None
            db.add(ChatMessage(
                customer_phone=to, direction="out", msg_type=msg_type, text=text or None, wa_message_id=wa_message_id,
                meta_media_id=media_id, output_id=output.id if output else None,
                ingestion_id=output.ingestion_id if output else None,
            ))
            try:
                db.commit()
            except IntegrityError:
                db.rollback()                      # the same send recorded twice
    except Exception as e:  # noqa: BLE001
        _fail(e)


def write_in(event: Dict[str, Any]) -> None:
    """Record one inbound webhook event (text, photo, button tap, form reply, anything else)."""
    kind = event.get("type")
    if kind in ("status", "payment_status"):
        return
    sender = normalize_phone(event.get("sender"))
    if not sender:
        return
    text: Optional[str] = None
    msg_type = "other"
    media_id = None
    if kind == "text":
        msg_type, text = "text", str(event.get("body", ""))
    elif kind == "image":
        msg_type, text, media_id = "image", str(event.get("caption", "") or ""), event.get("media_id") or None
    elif kind == "interactive" and event.get("subtype") == "button_reply":
        reply = event.get("button_reply") or {}
        msg_type, text = "button_reply", f"Tapped: {reply.get('title') or reply.get('id', '')}"
    elif kind == "interactive" and event.get("subtype") == "nfm_reply":
        msg_type, text = "flow", "Submitted the registration form"
    else:
        text = f"[{event.get('raw_type') or kind}]"
    try:
        with _session() as db:
            db.add(ChatMessage(customer_phone=sender, direction="in", msg_type=msg_type, text=text or None,
                               wa_message_id=event.get("message_id") or None, meta_media_id=media_id))
            try:
                db.commit()
            except IntegrityError:
                db.rollback()                      # Meta delivered the same message again
    except Exception as e:  # noqa: BLE001
        _fail(e)


def attach_photo(wa_message_id: str, sender: str, ingestion_id: str, image_id: Optional[str]) -> None:
    """Link the recorded inbound photo to its order and stored file (creating the record if the generic writer has
    not got there yet; the generic writer then finds it already present)."""
    if not wa_message_id:
        return
    try:
        with _session() as db:
            row = db.query(ChatMessage).filter(
                ChatMessage.direction == "in", ChatMessage.wa_message_id == wa_message_id).first()
            if row is None:
                row = ChatMessage(customer_phone=normalize_phone(sender), direction="in", msg_type="image",
                                  wa_message_id=wa_message_id)
                db.add(row)
            row.ingestion_id, row.image_id = ingestion_id, image_id
            try:
                db.commit()
                message_pk = row.id
            except IntegrityError:
                db.rollback()
                message_pk = None
        if message_pk and image_id:
            from app.services import drive_archive

            drive_archive.enqueue("photo", message_pk)
    except Exception as e:  # noqa: BLE001
        _fail(e)


def save_output(ingestion_id: str, style: Optional[str], position: int, data: bytes, mime_type: str = "image/png") -> Optional[str]:
    """Keep an image we produced for an order. Returns its id (None on any trouble). Blocking: use run_io."""
    if not enabled() or not data:
        return None
    ext = {"image/jpeg": "jpg", "image/webp": "webp"}.get(mime_type, "png")
    try:
        relative = Path("outputs") / ingestion_id / f"{uuid.uuid4().hex[:12]}.{ext}"
        target = settings.UPLOAD_PATH / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
        with _session() as db:
            row = OrderOutput(ingestion_id=ingestion_id, style=(style or "")[:80] or None, position=position,
                              file_path=relative.as_posix(), mime_type=mime_type, size_bytes=len(data))
            db.add(row)
            db.commit()
            output_id = row.id
        from app.services import drive_archive

        drive_archive.enqueue("output", output_id)       # archive copy (does nothing unless Drive is configured)
        return output_id
    except Exception as e:  # noqa: BLE001
        _fail(e)
        return None


def set_output_media(output_id: Optional[str], media_id: Optional[str]) -> None:
    if not output_id or not media_id:
        return
    try:
        with _session() as db:
            db.query(OrderOutput).filter(OrderOutput.id == output_id).update({"meta_media_id": media_id})
            db.commit()
    except Exception as e:  # noqa: BLE001
        _fail(e)


def upsert_invoice(payment_id: str, phone: str, amount: int, status: str, erpnext_invoice: Optional[str] = None) -> None:
    try:
        with _session() as db:
            row = db.query(InvoiceRecord).filter(InvoiceRecord.payment_id == payment_id).first()
            if row is None:
                row = InvoiceRecord(payment_id=payment_id, customer_phone=normalize_phone(phone), amount_rupees=int(amount),
                                    status=status, erpnext_invoice=erpnext_invoice)
                db.add(row)
            else:
                row.status = status
                if erpnext_invoice:
                    row.erpnext_invoice = erpnext_invoice
            db.commit()
    except Exception as e:  # noqa: BLE001
        _fail(e)
