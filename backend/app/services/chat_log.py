"""Writes the WhatsApp chat dashboard's record as things happen. See ``app/models/chat_log.py``.

Everything here is best effort and never raises into the order or message flow. Writes run on the I/O pool and are never
waited for (except keeping a produced image, which has its own short time limit), so a slow database cannot slow a
customer's chat. If the tables do not exist yet (database not upgraded) the writers switch themselves off for a minute;
any other error is logged and only that one write is lost.
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
_last_error_log = 0.0
_tasks: "set[asyncio.Task]" = set()
_suppressed: Dict[str, float] = {}


def reset_for_tests() -> None:
    global _disabled_until, _warned, _last_error_log
    _disabled_until, _warned, _last_error_log = 0.0, False, 0.0
    _suppressed.clear()


def enabled() -> bool:
    return bool(settings.CHAT_LOG_ENABLED) and time.monotonic() >= _disabled_until


def _is_missing_table(exc: Exception) -> bool:
    text = str(exc).lower()
    return any(m in text for m in ("no such table", "no such column", "does not exist", "undefinedtable", "undefinedcolumn"))


def _fail(exc: Exception) -> None:
    """A table-missing problem switches the writers off for a minute; any other error only loses that one write."""
    global _disabled_until, _warned, _last_error_log
    if _is_missing_table(exc):
        _disabled_until = time.monotonic() + _RETRY_AFTER_SECONDS
        if not _warned:
            _warned = True
            logger.warning("Chat log unavailable (tables missing); run `alembic upgrade head` to enable the dashboard record")
        return
    if time.monotonic() - _last_error_log > 60:
        _last_error_log = time.monotonic()
        logger.warning(f"Chat log write failed ({type(exc).__name__}); that entry is lost, later ones are still recorded")


def suppress(phone: str, seconds: float = 3600.0) -> None:
    """Stop recording this number for a while (used right after an erasure so the confirmation message does not bring
    the erased customer back into the dashboard)."""
    _suppressed[normalize_phone(phone)] = time.monotonic() + seconds


def _is_suppressed(phone: str) -> bool:
    until = _suppressed.get(phone)
    if until is None:
        return False
    if until < time.monotonic():
        _suppressed.pop(phone, None)
        return False
    return True


def _session():
    from app.database import SessionLocal

    return SessionLocal()


def fire(fn, *args: Any) -> None:
    """Run a blocking writer on the I/O pool without waiting for it. Does nothing outside a running event loop."""
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


def fire_disk(fn, *args: Any) -> None:
    """Like ``fire`` but on the disk pool: for jobs that carry an image. The caller hands the image over and lets go of it;
    the job releases it as soon as it is on disk."""
    if not enabled():
        return

    async def _go() -> None:
        from app.utils.executors import run_disk

        try:
            await run_disk(fn, *args)
        except Exception:  # noqa: BLE001
            pass

    try:
        task = asyncio.ensure_future(_go())
    except RuntimeError:
        return
    _tasks.add(task)
    task.add_done_callback(_tasks.discard)


def keep_output(ingestion_id: str, style: Optional[str], data: bytes, media_id: Optional[str]) -> Optional[str]:
    """Write a produced image to disk and record it. Blocking (use ``fire_disk``). The image is released on return."""
    info = write_output_file(ingestion_id, data)
    return register_output(ingestion_id, style, 0, info, media_id) if info else None


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
    if not to or _is_suppressed(to):
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
    if not sender or _is_suppressed(sender):
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
                db.rollback()
                # The photo link may have created the row first: fill in what only this writer knows.
                row = db.query(ChatMessage).filter(
                    ChatMessage.direction == "in", ChatMessage.wa_message_id == event.get("message_id")).first()
                if row is not None and kind == "image" and (not row.text or not row.meta_media_id):
                    row.text = row.text or (text or None)
                    row.meta_media_id = row.meta_media_id or media_id
                    db.commit()
    except Exception as e:  # noqa: BLE001
        _fail(e)


def attach_photo(wa_message_id: str, sender: str, ingestion_id: str, image_id: Optional[str]) -> None:
    """Link the recorded inbound photo to its order and stored file (creating the record if the generic writer has
    not got there yet; the generic writer then finds it already present and fills in the rest)."""
    phone = normalize_phone(sender)
    if not wa_message_id or _is_suppressed(phone):
        return
    try:
        with _session() as db:
            message_pk: Optional[str] = None
            for _ in range(2):                     # a second pass if the generic writer inserted it between our look and our insert
                row = db.query(ChatMessage).filter(
                    ChatMessage.direction == "in", ChatMessage.wa_message_id == wa_message_id).first()
                if row is None:
                    row = ChatMessage(customer_phone=phone, direction="in", msg_type="image", wa_message_id=wa_message_id)
                    db.add(row)
                row.ingestion_id, row.image_id = ingestion_id, image_id
                try:
                    db.commit()
                    message_pk = row.id
                    break
                except IntegrityError:
                    db.rollback()
        if message_pk and image_id:
            from app.services import drive_archive

            drive_archive.enqueue("photo", message_pk)
    except Exception as e:  # noqa: BLE001
        _fail(e)


def _image_type(data: bytes, default: str) -> Tuple[str, str]:
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return "image/png", "png"
    if data[:3] == b"\xff\xd8\xff":
        return "image/jpeg", "jpg"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp", "webp"
    return default, {"image/jpeg": "jpg", "image/webp": "webp"}.get(default, "png")


def write_output_file(ingestion_id: str, data: bytes, mime_type: str = "image/png") -> Optional[Dict[str, Any]]:
    """Step 1 of keeping a produced image: put the bytes on disk. Blocking (use ``run_disk``). Returns what step 2 needs
    (never the image itself, so the image can be released as soon as this returns), or None on any trouble."""
    if not enabled() or not data:
        return None
    mime_type, ext = _image_type(data, mime_type)
    try:
        relative = Path("outputs") / ingestion_id / f"{uuid.uuid4().hex[:12]}.{ext}"
        target = settings.UPLOAD_PATH / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
        return {"file_path": relative.as_posix(), "mime_type": mime_type, "size_bytes": len(data)}
    except Exception as e:  # noqa: BLE001
        _fail(e)
        return None


def register_output(ingestion_id: str, style: Optional[str], position: int, info: Dict[str, Any],
                    media_id: Optional[str] = None) -> Optional[str]:
    """Step 2: record the kept file in the database (and, when the Meta media id is known, connect any message already
    recorded for it and queue the Drive copy). Blocking. Returns the output's id."""
    try:
        with _session() as db:
            row = OrderOutput(ingestion_id=ingestion_id, style=(style or "")[:80] or None, position=position,
                              file_path=info["file_path"], mime_type=info["mime_type"], size_bytes=info["size_bytes"],
                              meta_media_id=media_id or None)
            db.add(row)
            if media_id:
                db.query(ChatMessage).filter(
                    ChatMessage.direction == "out", ChatMessage.meta_media_id == media_id, ChatMessage.output_id.is_(None)
                ).update({"output_id": row.id, "ingestion_id": ingestion_id}, synchronize_session=False)
            db.commit()
            output_id = row.id
        from app.services import drive_archive

        drive_archive.enqueue("output", output_id)       # archive copy (does nothing unless Drive is configured)
        return output_id
    except Exception as e:  # noqa: BLE001
        _fail(e)
        return None


def save_output(ingestion_id: str, style: Optional[str], position: int, data: bytes, mime_type: str = "image/png") -> Optional[str]:
    """Both steps in one blocking call (used by tests and scripts). Returns the output's id, None on any trouble."""
    info = write_output_file(ingestion_id, data, mime_type)
    return register_output(ingestion_id, style, position, info) if info else None


def link_output_media(output_id: Optional[str], media_id: Optional[str]) -> None:
    """After upload: remember which Meta media id this kept image became, connect any message already recorded for that
    media id, and queue the Drive archive copy."""
    if not output_id or not media_id:
        return
    try:
        with _session() as db:
            output = db.get(OrderOutput, output_id)
            if output is None:
                return
            output.meta_media_id = media_id
            db.query(ChatMessage).filter(
                ChatMessage.direction == "out", ChatMessage.meta_media_id == media_id, ChatMessage.output_id.is_(None)
            ).update({"output_id": output_id, "ingestion_id": output.ingestion_id}, synchronize_session=False)
            db.commit()
        from app.services import drive_archive

        drive_archive.enqueue("output", output_id)
    except Exception as e:  # noqa: BLE001
        _fail(e)


def upsert_invoice(payment_id: str, phone: str, amount: int, status: str, erpnext_invoice: Optional[str] = None,
                   drive_file_id: Optional[str] = None, drive_link: Optional[str] = None) -> None:
    try:
        with _session() as db:
            for _ in range(2):                     # a second pass if a concurrent retry inserted it first
                row = db.query(InvoiceRecord).filter(InvoiceRecord.payment_id == payment_id).first()
                if row is None:
                    row = InvoiceRecord(payment_id=payment_id, customer_phone=normalize_phone(phone), amount_rupees=int(amount),
                                        status=status, erpnext_invoice=erpnext_invoice, drive_file_id=drive_file_id,
                                        drive_link=drive_link)
                    db.add(row)
                else:
                    row.status = status
                    if erpnext_invoice:
                        row.erpnext_invoice = erpnext_invoice
                    if drive_file_id:
                        row.drive_file_id, row.drive_link = drive_file_id, drive_link
                try:
                    db.commit()
                    break
                except IntegrityError:
                    db.rollback()
    except Exception as e:  # noqa: BLE001
        _fail(e)
