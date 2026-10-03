"""Archive customer photos and the images we produced to Google Drive (for the chat dashboard).

Off until DRIVE_ENABLED=true and the Google credentials are set. Runs as durable outbox jobs ("drive_archive") after a
file is saved, never during a customer's chat: a slow or failing Drive only delays the archive copy, retried with growing
waits like every outbox job. Uses Google's REST API directly (no extra libraries) with an OAuth refresh token of the Drive
owner's account, so a personal Google account with a big plan works (a robot account has no storage of its own).

Settings: DRIVE_ENABLED, GOOGLE_DRIVE_CLIENT_ID, GOOGLE_DRIVE_CLIENT_SECRET, GOOGLE_DRIVE_REFRESH_TOKEN, DRIVE_FOLDER_ID.
The dashboard shows the local copy as a preview and links to the Drive copy for zoom and download.
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any, Dict, Optional

import httpx

from app.config import settings
from app.utils.executors import run_io
from app.utils.logger import logger

TOKEN_URL = "https://oauth2.googleapis.com/token"
UPLOAD_URL = "https://www.googleapis.com/upload/drive/v3/files"
OUTBOX_KIND = "drive_archive"

_token: Dict[str, Any] = {"value": None, "expires": 0.0}


def configured() -> bool:
    return bool(settings.DRIVE_ENABLED and settings.GOOGLE_DRIVE_CLIENT_ID and settings.GOOGLE_DRIVE_CLIENT_SECRET
                and settings.GOOGLE_DRIVE_REFRESH_TOKEN)


def reset_for_tests() -> None:
    _token.update(value=None, expires=0.0)


async def _access_token(client: httpx.AsyncClient) -> str:
    if _token["value"] and time.monotonic() < _token["expires"] - 60:
        return _token["value"]
    response = await client.post(TOKEN_URL, data={
        "client_id": settings.GOOGLE_DRIVE_CLIENT_ID, "client_secret": settings.GOOGLE_DRIVE_CLIENT_SECRET,
        "refresh_token": settings.GOOGLE_DRIVE_REFRESH_TOKEN, "grant_type": "refresh_token",
    })
    if response.status_code != 200:
        raise RuntimeError(f"Google refused the Drive credentials (HTTP {response.status_code})")
    data = response.json()
    _token.update(value=data["access_token"], expires=time.monotonic() + float(data.get("expires_in", 3000)))
    return _token["value"]


async def upload_file(name: str, data: bytes, mime_type: str) -> Dict[str, str]:
    """Upload one file to the configured folder. Returns {"id", "link"}. Raises on any failure (the outbox retries)."""
    metadata: Dict[str, Any] = {"name": name}
    if settings.DRIVE_FOLDER_ID:
        metadata["parents"] = [settings.DRIVE_FOLDER_ID]
    boundary = "moraa-drive-boundary"
    body = (
        f"--{boundary}\r\nContent-Type: application/json; charset=UTF-8\r\n\r\n{json.dumps(metadata)}\r\n"
        f"--{boundary}\r\nContent-Type: {mime_type}\r\n\r\n"
    ).encode("utf-8") + data + f"\r\n--{boundary}--".encode("utf-8")
    async with httpx.AsyncClient(timeout=60.0) as client:
        token = await _access_token(client)
        response = await client.post(
            UPLOAD_URL, params={"uploadType": "multipart", "fields": "id,webViewLink", "supportsAllDrives": "true"},
            headers={"Authorization": f"Bearer {token}", "Content-Type": f"multipart/related; boundary={boundary}"},
            content=body,
        )
    if response.status_code not in (200, 201):
        raise RuntimeError(f"Drive upload failed (HTTP {response.status_code})")
    info = response.json()
    return {"id": info["id"], "link": info.get("webViewLink") or f"https://drive.google.com/file/d/{info['id']}/view"}


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
    data = await run_io(lambda: Path(job["path"]).read_bytes())
    info = await upload_file(job["name"], data, job["mime"])
    await run_io(_save, kind, item_id, info)
    logger.bind(category="system").info(f"Archived a {kind} to Drive")
    return True


def delete_files_sync(ids) -> int:
    """(blocking) Delete archived copies from Drive (retention and erasure). Best effort: a file that is already gone
    counts as deleted; any other failure is logged and that copy stays (the dashboard no longer lists it). Returns how
    many are gone."""
    ids = [i for i in ids if i]
    if not ids or not configured():
        return 0
    deleted = 0
    try:
        with httpx.Client(timeout=30.0) as client:
            token = client.post(TOKEN_URL, data={
                "client_id": settings.GOOGLE_DRIVE_CLIENT_ID, "client_secret": settings.GOOGLE_DRIVE_CLIENT_SECRET,
                "refresh_token": settings.GOOGLE_DRIVE_REFRESH_TOKEN, "grant_type": "refresh_token",
            })
            if token.status_code != 200:
                logger.warning("Drive copies could not be deleted: Google refused the credentials")
                return 0
            headers = {"Authorization": f"Bearer {token.json()['access_token']}"}
            for file_id in ids:
                response = client.delete(f"https://www.googleapis.com/drive/v3/files/{file_id}",
                                         headers=headers, params={"supportsAllDrives": "true"})
                if response.status_code in (200, 204, 404):
                    deleted += 1
                else:
                    logger.warning(f"A Drive copy could not be deleted (HTTP {response.status_code})")
    except Exception as e:  # noqa: BLE001
        logger.warning(f"Drive copies could not be deleted ({type(e).__name__})")
    return deleted


def enqueue(kind: str, item_id: str) -> None:
    """(blocking) Ask the outbox to archive this file. Never raises."""
    if not configured():
        return
    from app.services import outbox

    outbox.enqueue_job(OUTBOX_KIND, {"kind": kind, "id": item_id}, f"drive:{kind}:{item_id}")


async def handle_outbox_job(payload: Dict[str, Any]) -> bool:
    return await archive(payload["kind"], payload["id"])
