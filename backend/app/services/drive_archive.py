"""Archive customer photos and the images we produced to Google Drive (for the chat dashboard).

Off until DRIVE_ENABLED=true and the business service account is set up (GOOGLE_SA_KEY_FILE, DRIVE_SHARED_DRIVE_ID;
see app/services/google_drive.py, the same account and shared drive as the Phase 8 customer folders). Runs as durable
outbox jobs ("drive_archive") after a file is saved, never during a customer's chat: a slow or failing Drive only
delays the archive copy, retried with growing waits like every outbox job. Files go into DRIVE_FOLDER_ID (a folder
inside the shared drive), else the top of the shared drive, streamed from disk.

Settings: DRIVE_ENABLED, DRIVE_FOLDER_ID (plus the service-account settings above).
The dashboard shows the local copy as a preview and links to the Drive copy for zoom and download.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, Optional

from app.config import settings
from app.services import google_drive
from app.utils.executors import run_io
from app.utils.logger import logger

OUTBOX_KIND = "drive_archive"


def configured() -> bool:
    return bool(settings.DRIVE_ENABLED) and google_drive.configured()


def reset_for_tests() -> None:
    google_drive.reset_for_tests()


async def upload_file(path: str, name: str, mime_type: str) -> Dict[str, str]:
    """Upload one file (streamed from disk) to the archive folder. Returns {"id", "link"}. Raises
    google_drive.DriveError on any failure (the outbox retries)."""
    folder = (settings.DRIVE_FOLDER_ID or "").strip() or google_drive.shared_drive_id()
    return await google_drive.upload_file(path, name, folder, mime_type)


def _load(kind: str, item_id: str) -> Optional[Dict[str, Any]]:
    """(blocking) The file to archive and a good name for it, or None when there is nothing to do."""
    from app.database import SessionLocal
    from app.models.chat_log import ChatMessage, OrderOutput
    from app.services.dashboard_service import resolve_media_file

    with SessionLocal() as db:
        if kind == "output":
            row = db.get(OrderOutput, item_id)
            if row is None or row.drive_file_id or not row.file_path:
                return None
            found = resolve_media_file(db, "output", item_id)
            name = f"{row.ingestion_id}_{(row.style or 'image').replace(' ', '_')}_{row.id[:6]}{Path(row.file_path).suffix}"
        else:
            msg = db.get(ChatMessage, item_id)
            if msg is None or msg.drive_file_id or not msg.image_id:
                return None
            found = resolve_media_file(db, "photo", msg.image_id)
            name = f"{msg.customer_phone}_{msg.ingestion_id or msg.id[:8]}_input{Path(found['path']).suffix if found else '.jpg'}"
        if not found:
            return None
        return {"path": found["path"], "mime": found["mime"], "name": name}


def _save(kind: str, item_id: str, info: Dict[str, str]) -> None:
    from app.database import SessionLocal
    from app.models.chat_log import ChatMessage, OrderOutput

    with SessionLocal() as db:
        model = OrderOutput if kind == "output" else ChatMessage
        db.query(model).filter(model.id == item_id).update({"drive_file_id": info["id"], "drive_link": info["link"]})
        db.commit()


async def archive(kind: str, item_id: str) -> bool:
    """Archive one file. True when done (or nothing to do); raises when Drive failed, so the outbox retries."""
    if not configured():
        return True
    job = await run_io(_load, kind, item_id)
    if job is None:
        return True
    info = await upload_file(job["path"], job["name"], job["mime"])
    await run_io(_save, kind, item_id, info)
    logger.bind(category="system").info(f"Archived a {kind} to Drive")
    return True


def delete_files_sync(ids) -> int:
    """(blocking) Delete archived copies from Drive (retention and erasure). Best effort: a file that is already gone
    counts as deleted; any other failure is logged and that copy stays (the dashboard no longer lists it). Returns how
    many are gone. Works whenever the service account is set up, so copies made while the archive was on are still
    removed after DRIVE_ENABLED is switched off."""
    return google_drive.delete_files_sync(ids)


def enqueue(kind: str, item_id: str) -> None:
    """(blocking) Ask the outbox to archive this file. Never raises."""
    if not configured():
        return
    from app.services import outbox

    outbox.enqueue_job(OUTBOX_KIND, {"kind": kind, "id": item_id}, f"drive:{kind}:{item_id}")


async def handle_outbox_job(payload: Dict[str, Any]) -> bool:
    return await archive(payload["kind"], payload["id"])
