"""Google Drive and Sheets over REST with the business service account (Phase 8).

Everything lives in the business Google Workspace shared drive (DRIVE_SHARED_DRIVE_ID); the service account is a
member of it, so files never need storage of their own (a service account has none). There is no personal OAuth.

The token is a signed JWT (RS256, the key from GOOGLE_SA_KEY_FILE) exchanged at Google's token endpoint and reused
until a minute before it expires. Neither the key nor the token is ever logged.

Every call goes through ``_request``: a rate limit (429, 403 rateLimitExceeded / userRateLimitExceeded) or a Google
5xx is retried twice here with jittered back-off and then raised as ``DriveError(retryable=True)``, so the outbox job
retries later; any other 4xx is ``DriveError(retryable=False)``. Sharing only ever creates "user" permissions:
there is no code path that makes a file public ("anyone") or domain-wide.

Tests put an ``httpx.MockTransport`` in ``_transport``; production leaves it None.
"""

from __future__ import annotations

import asyncio
import json
import os
import random
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

import httpx
from jose import jwt

from app.config import settings
from app.utils.executors import run_io
from app.utils.logger import logger

FILES_URL = "https://www.googleapis.com/drive/v3/files"
UPLOAD_URL = "https://www.googleapis.com/upload/drive/v3/files"
SHEETS_URL = "https://sheets.googleapis.com/v4/spreadsheets"
SCOPES = "https://www.googleapis.com/auth/drive https://www.googleapis.com/auth/spreadsheets"
FOLDER_MIME = "application/vnd.google-apps.folder"
SHEET_MIME = "application/vnd.google-apps.spreadsheet"

TIMEOUT_SECONDS = 60.0
CHUNK_SIZE = 8 * 256 * 1024             # resumable upload chunks must be multiples of 256 KiB
MAX_RETRIES = 2                         # in-call retries; after that the outbox retries the whole job
BACKOFF_SECONDS = 1.0                   # first in-call wait (doubles, with jitter)
RATE_LIMIT_REASONS = frozenset({"rateLimitExceeded", "userRateLimitExceeded"})
SHARE_ROLES = frozenset({"reader", "commenter", "writer"})

_transport: Optional[httpx.MockTransport] = None
_key: Dict[str, Any] = {"path": None, "value": None}
_token: Dict[str, Any] = {"value": None, "expires": 0.0}
_lock: Dict[str, Any] = {"loop": None, "lock": None}


class DriveError(Exception):
    """A Google API call failed. ``retryable`` tells the outbox job to raise (try again later) or give up."""

    def __init__(self, status: int, reason: str = "", retryable: bool = False) -> None:
        super().__init__(f"Google API error {status} ({reason or 'no reason given'})")
        self.status = status
        self.reason = reason
        self.retryable = retryable


def reset_for_tests() -> None:
    _key.update(path=None, value=None)
    _token.update(value=None, expires=0.0)
    _lock.update(loop=None, lock=None)


def _service_key() -> Optional[Dict[str, str]]:
    """The parsed service-account key (client_email, private_key, token_uri), cached per path; None when unusable."""
    path = (settings.GOOGLE_SA_KEY_FILE or "").strip()
    if not path:
        return None
    if _key["path"] != path:
        try:
            data = json.loads(Path(path).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            data = None
        fields = ("client_email", "private_key", "token_uri")
        value = {f: str(data[f]) for f in fields} if isinstance(data, dict) and all(data.get(f) for f in fields) else None
        if value is None:
            logger.warning("GOOGLE_SA_KEY_FILE is not a readable service-account JSON key: Google Drive stays off")
        _key.update(path=path, value=value)
    return _key["value"]


def configured() -> bool:
    """True when the service-account key is readable and the shared drive is set."""
    return bool((settings.DRIVE_SHARED_DRIVE_ID or "").strip()) and _service_key() is not None


def shared_drive_id() -> str:
    return (settings.DRIVE_SHARED_DRIVE_ID or "").strip()


def folder_link(folder_id: str) -> str:
    return f"https://drive.google.com/drive/folders/{folder_id}"


def file_link(file_id: str) -> str:
    return f"https://drive.google.com/file/d/{file_id}/view"


def _check(*values: Any) -> None:
    for value in values:
        if not isinstance(value, str) or not value.strip():
            raise ValueError("a Google Drive name or id must be a non-empty string")


# ── token ──────────────────────────────────────────────────────────────────────────────────────────────────

def _token_form() -> Dict[str, str]:
    key = _service_key()
    if key is None:
        raise DriveError(0, "notConfigured", retryable=False)
    now = int(time.time())
    claims = {"iss": key["client_email"], "scope": SCOPES, "aud": key["token_uri"], "iat": now, "exp": now + 3600}
    return {"grant_type": "urn:ietf:params:oauth:grant-type:jwt-bearer",
            "assertion": jwt.encode(claims, key["private_key"], algorithm="RS256")}


def _cached_token() -> Optional[str]:
    return _token["value"] if _token["value"] and time.monotonic() < _token["expires"] - 60 else None


def _remember_token(response: httpx.Response) -> str:
    if response.status_code != 200:
        # Retryable: a wrong key then ends in the outbox's dead-job fallback (WhatsApp), a clock or Google hiccup heals.
        raise DriveError(response.status_code, "tokenRefused", retryable=True)
    data = response.json()
    _token.update(value=data["access_token"], expires=time.monotonic() + float(data.get("expires_in", 3600)))
    return _token["value"]


def _token_lock() -> asyncio.Lock:
    loop = asyncio.get_running_loop()
    if _lock["loop"] is not loop:
        _lock.update(loop=loop, lock=asyncio.Lock())
    return _lock["lock"]


async def _access_token() -> str:
    cached = _cached_token()
    if cached:
        return cached
    async with _token_lock():                        # one token request, however many calls are waiting
        cached = _cached_token()
        if cached:
            return cached
        form = _token_form()
        try:
            async with httpx.AsyncClient(timeout=TIMEOUT_SECONDS, transport=_transport) as client:
                response = await client.post(_service_key()["token_uri"], data=form)
        except httpx.TransportError as e:
            raise DriveError(0, type(e).__name__, retryable=True) from None
        return _remember_token(response)


# ── the one request helper ─────────────────────────────────────────────────────────────────────────────────

def _error(response: httpx.Response) -> DriveError:
    try:
        data = response.json()
    except ValueError:
        data = None
    body = data.get("error") if isinstance(data, dict) else None
    body = body if isinstance(body, dict) else {}
    errors = body.get("errors") if isinstance(body.get("errors"), list) else []
    reason = str((errors[0].get("reason") if errors and isinstance(errors[0], dict) else None) or body.get("status") or "")
    status = response.status_code
    retryable = status in (401, 429) or status >= 500 or (status == 403 and reason in RATE_LIMIT_REASONS)
    return DriveError(status, reason, retryable)


async def _request(method: str, url: str, *, params: Optional[Dict[str, Any]] = None, json_body: Any = None,
                   content: Optional[bytes] = None, headers: Optional[Dict[str, str]] = None,
                   missing_ok: bool = False) -> httpx.Response:
    params = dict(params or {})
    if "/drive/" in url:                             # Drive only: the Sheets API refuses unknown parameters
        params["supportsAllDrives"] = "true"
    target = httpx.URL(url).copy_merge_params(params)   # keeps a query already in the URL (the upload session id)
    for attempt in range(MAX_RETRIES + 1):
        token = await _access_token()
        try:
            async with httpx.AsyncClient(timeout=TIMEOUT_SECONDS, transport=_transport) as client:
                response = await client.request(method, target, json=json_body, content=content,
                                                headers={"Authorization": f"Bearer {token}", **(headers or {})})
        except httpx.TransportError as e:
            error = DriveError(0, type(e).__name__, retryable=True)
        else:
            if response.status_code < 400 or (missing_ok and response.status_code == 404):
                return response
            error = _error(response)
            if response.status_code == 401:
                _token.update(value=None, expires=0.0)  # revoked or expired early: fetch a new one
        if not error.retryable or attempt == MAX_RETRIES:
            raise error
        await asyncio.sleep(BACKOFF_SECONDS * (2 ** attempt) * random.uniform(0.5, 1.5))
    raise AssertionError("unreachable")


# ── files and folders ──────────────────────────────────────────────────────────────────────────────────────

async def _create(name: str, parent_id: str, mime: str) -> str:
    _check(name, parent_id)
    response = await _request("POST", FILES_URL, params={"fields": "id"},
                              json_body={"name": name, "mimeType": mime, "parents": [parent_id]})
    return response.json()["id"]


async def create_folder(name: str, parent_id: str) -> str:
    return await _create(name, parent_id, FOLDER_MIME)


async def create_sheet(name: str, parent_id: str) -> str:
    """A Google Sheet made through Drive (files.create), so it lives in the shared drive like everything else."""
    return await _create(name, parent_id, SHEET_MIME)


def _quote(value: str) -> str:
    return value.replace("\\", "\\\\").replace("'", "\\'")


async def _files(query: str) -> List[Dict[str, Any]]:
    found: List[Dict[str, Any]] = []
    page: Optional[str] = None
    while True:
        params = {"q": query, "corpora": "drive", "driveId": shared_drive_id(), "includeItemsFromAllDrives": "true",
                  "fields": "nextPageToken,files(id,name,mimeType)", "pageSize": "1000"}
        if page:
            params["pageToken"] = page
        data = (await _request("GET", FILES_URL, params=params)).json()
        found += data.get("files") or []
        page = data.get("nextPageToken")
        if not page:
            return found


async def find_child(name: str, parent_id: str, mime: Optional[str] = None) -> Optional[str]:
    """Id of the first non-trashed item called ``name`` directly inside ``parent_id`` (optionally of ``mime``)."""
    _check(name, parent_id)
    query = f"name = '{_quote(name)}' and '{_quote(parent_id)}' in parents and trashed = false"
    if mime:
        query += f" and mimeType = '{_quote(mime)}'"
    files = await _files(query)
    return files[0]["id"] if files else None


async def list_children(parent_id: str) -> List[Dict[str, Any]]:
    """Every non-trashed item directly inside ``parent_id``: [{"id", "name", "mimeType"}]."""
    _check(parent_id)
    return await _files(f"'{_quote(parent_id)}' in parents and trashed = false")


def _read_chunk(path: str, offset: int, size: int) -> bytes:
    with open(path, "rb") as f:
        f.seek(offset)
        return f.read(size)


async def upload_file(path: str, name: str, parent_id: str, mime: str) -> Dict[str, str]:
    """Upload a file with a resumable upload, streamed from disk in CHUNK_SIZE pieces (never the whole file in
    memory, no size limit). Returns {"id", "link"}."""
    _check(str(path), name, parent_id, mime)
    size = await run_io(os.path.getsize, path)
    start = await _request("POST", UPLOAD_URL, params={"uploadType": "resumable", "fields": "id,webViewLink"},
                           json_body={"name": name, "mimeType": mime, "parents": [parent_id]},
                           headers={"X-Upload-Content-Type": mime, "X-Upload-Content-Length": str(size)})
    session = start.headers.get("location")
    if not session:
        raise DriveError(start.status_code, "noUploadSession", retryable=True)
    offset = 0
    while True:
        chunk = await run_io(_read_chunk, str(path), offset, CHUNK_SIZE)
        content_range = f"bytes {offset}-{offset + len(chunk) - 1}/{size}" if chunk else f"bytes */{size}"
        try:
            response = await _request("PUT", session, content=chunk, headers={"Content-Range": content_range})
        except DriveError as e:
            if e.status in (404, 410):               # the upload session expired: the outbox starts a new one
                raise DriveError(e.status, "uploadSessionExpired", retryable=True) from None
            raise
        if response.status_code in (200, 201):
            info = response.json()
            return {"id": info["id"], "link": info.get("webViewLink") or file_link(info["id"])}
        received = response.headers.get("range")    # 308: "bytes=0-N" is what Google has so far
        next_offset = int(received.rsplit("-", 1)[1]) + 1 if received else 0
        if next_offset <= offset:
            raise DriveError(response.status_code, "uploadStalled", retryable=True)
        offset = next_offset


async def rename(file_id: str, name: str) -> None:
    _check(file_id, name)
    await _request("PATCH", f"{FILES_URL}/{file_id}", params={"fields": "id"}, json_body={"name": name})


async def delete(file_id: str) -> None:
    """Delete a file or folder (with everything in it). Already gone counts as done. A Content manager may not delete
    for good in a shared drive, so a 403 moves it to the trash instead (Google empties it after 30 days)."""
    _check(file_id)
    try:
        await _request("DELETE", f"{FILES_URL}/{file_id}", missing_ok=True)
    except DriveError as e:
        if e.status != 403 or e.retryable:
            raise
        await _request("PATCH", f"{FILES_URL}/{file_id}", params={"fields": "id"}, json_body={"trashed": True},
                       missing_ok=True)


# ── permissions ────────────────────────────────────────────────────────────────────────────────────────────

async def share_with_email(file_id: str, email: str, role: str = "reader") -> str:
    """Give one Google account access. Returns the permission id. Only ever a "user" permission."""
    _check(file_id, email)
    if role not in SHARE_ROLES or "@" not in email:
        raise ValueError("share_with_email needs an email address and a reader/commenter/writer role")
    response = await _request("POST", f"{FILES_URL}/{file_id}/permissions", params={"fields": "id"},
                              json_body={"type": "user", "role": role, "emailAddress": email})
    return response.json()["id"]


async def list_permissions(file_id: str) -> List[Dict[str, Any]]:
    """[{"id", "type", "role", "emailAddress", "permissionDetails"}] (inherited shared-drive members included)."""
    _check(file_id)
    response = await _request("GET", f"{FILES_URL}/{file_id}/permissions",
                              params={"fields": "permissions(id,type,role,emailAddress,permissionDetails)"})
    return response.json().get("permissions") or []


async def remove_permission(file_id: str, permission_id: str) -> None:
    _check(file_id, permission_id)
    await _request("DELETE", f"{FILES_URL}/{file_id}/permissions/{permission_id}", missing_ok=True)


# ── sheets ─────────────────────────────────────────────────────────────────────────────────────────────────

async def sheet_values(sheet_id: str, cell_range: str) -> List[List[Any]]:
    """The rows in ``cell_range`` of the first tab (e.g. "A:A"); trailing empty rows are not returned."""
    _check(sheet_id, cell_range)
    response = await _request("GET", f"{SHEETS_URL}/{sheet_id}/values/{cell_range}")
    return response.json().get("values") or []


async def sheet_append(sheet_id: str, rows: List[List[Any]]) -> None:
    """Append ``rows`` after the last row of the first tab, in one call. Values are stored as given (RAW)."""
    _check(sheet_id)
    if not rows:
        return
    await _request("POST", f"{SHEETS_URL}/{sheet_id}/values/A1:append",
                   params={"valueInputOption": "RAW", "insertDataOption": "INSERT_ROWS"}, json_body={"values": rows})


async def sheet_clear(sheet_id: str) -> None:
    _check(sheet_id)
    await _request("POST", f"{SHEETS_URL}/{sheet_id}/values/A:Z:clear", json_body={})


# ── blocking delete for retention (runs in threads) ────────────────────────────────────────────────────────

def delete_files_sync(ids) -> int:
    """(blocking) Delete files for retention and erasure. Best effort: already gone counts as deleted, any other
    failure is logged and that file stays. Returns how many are gone. Does nothing unless configured()."""
    ids = [i for i in ids if i]
    if not ids or not configured():
        return 0
    deleted = 0
    try:
        with httpx.Client(timeout=TIMEOUT_SECONDS, transport=_transport) as client:
            token = _cached_token() or _remember_token(client.post(_service_key()["token_uri"], data=_token_form()))
            headers = {"Authorization": f"Bearer {token}"}
            params = {"supportsAllDrives": "true"}
            for file_id in ids:
                url = f"{FILES_URL}/{file_id}"
                status = client.delete(url, headers=headers, params=params).status_code
                if status == 403:                    # Content manager: trash it instead (see delete())
                    status = client.patch(url, headers=headers, params=params, json={"trashed": True}).status_code
                if status in (200, 204, 404):
                    deleted += 1
                else:
                    logger.warning(f"A Drive file could not be deleted (HTTP {status})")
    except Exception as e:  # noqa: BLE001
        logger.warning(f"Drive files could not be deleted ({type(e).__name__})")
    return deleted
