"""Phase 8 Drive core: the service-account REST client (google_drive) and each customer's folders (drive_layout).

``FakeGoogle`` is the one in-memory fake of the Google token endpoint, Drive v3 (files, permissions, resumable
upload) and Sheets v4 values, served through ``httpx.MockTransport``. tests/test_usage_log.py and
tests/test_drive_archive.py use it too.
"""

from __future__ import annotations

import asyncio
import functools
import json
import os
import re
import tempfile
import unittest
import urllib.parse
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from unittest.mock import AsyncMock, patch

import httpx
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from jose import jwt
from sqlalchemy.orm import sessionmaker

import app.models  # noqa: F401 -- register every model with Base.metadata
from app.config import settings
from app.database import Base
from app.models.customer import Customer
from app.models.outbox_job import OutboxJob
from app.services import drive_layout, google_drive, metrics
from app.services.google_drive import FOLDER_MIME, SHEET_MIME, DriveError
from tests.db_support import make_engine

TOKEN_URI = "https://oauth2.fake-google.test/token"
SA_EMAIL = "moraa-drive@moraa-test.iam.gserviceaccount.com"
DRIVE_ID = "shared-drive-1"
PHONE = "919812345678"


@functools.lru_cache(maxsize=1)
def _rsa_key():
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


def private_pem() -> str:
    return _rsa_key().private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                    serialization.NoEncryption()).decode()


def public_pem() -> str:
    return _rsa_key().public_key().public_bytes(serialization.Encoding.PEM,
                                                serialization.PublicFormat.SubjectPublicKeyInfo).decode()


def _error(status: int, reason: str) -> httpx.Response:
    return httpx.Response(status, json={"error": {"code": status, "message": "fake", "errors": [{"reason": reason}]}})


def _unescape(value: str) -> str:
    return re.sub(r"\\(.)", r"\1", value)


class FakeGoogle:
    """In-memory Google: token endpoint, Drive v3 files/permissions/resumable upload, Sheets v4 values."""

    def __init__(self) -> None:
        self.files: Dict[str, Dict[str, Any]] = {
            DRIVE_ID: {"name": "Moraa Customers", "mimeType": FOLDER_MIME, "parents": [], "trashed": False},
        }
        self.permissions: Dict[str, List[Dict[str, Any]]] = {}
        self.sheets: Dict[str, List[List[Any]]] = {}
        self.sessions: Dict[str, Dict[str, Any]] = {}
        self.calls: List[Tuple[str, str]] = []           # every API call (method, path), not the token
        self.created: List[Tuple[str, str]] = []         # (name, mimeType) of every files.create / upload
        self.deleted: List[str] = []
        self.permission_requests: List[Dict[str, Any]] = []
        self.chunks: List[int] = []                       # bytes received per resumable PUT
        self.token_requests = 0
        self.claims: Optional[Dict[str, Any]] = None
        self.refuse_token = False
        self.organizer = True                             # False: a Content manager, permanent delete refused
        self.refused_emails: set = set()
        self.failures: List[Tuple[str, str, int, str]] = []   # (method, path part, status, reason), each used once
        self._counter = 0

    # ── helpers for tests ──
    def new_id(self, prefix: str) -> str:
        self._counter += 1
        return f"{prefix}{self._counter}"

    def add_folder(self, name: str, parent: str = DRIVE_ID) -> str:
        folder_id = self.new_id("folder")
        self.files[folder_id] = {"name": name, "mimeType": FOLDER_MIME, "parents": [parent], "trashed": False}
        return folder_id

    def children(self, parent: str) -> List[str]:
        return [i for i, f in self.files.items() if parent in f["parents"] and not f["trashed"]]

    def fail(self, method: str, path_part: str, status: int, reason: str = "backendError", times: int = 1) -> None:
        self.failures += [(method, path_part, status, reason)] * times

    def count(self, method: str, path_part: str) -> int:
        return len([c for c in self.calls if c[0] == method and path_part in c[1]])

    # ── transport ──
    def handler(self, request: httpx.Request) -> httpx.Response:
        if str(request.url).startswith(TOKEN_URI):
            return self._token(request)
        if not request.headers.get("authorization", "").startswith("Bearer tok"):
            return _error(401, "authError")
        path = request.url.path
        self.calls.append((request.method, path))
        for i, (method, part, status, reason) in enumerate(self.failures):
            if method == request.method and part in path:
                del self.failures[i]
                return _error(status, reason)
        if request.url.host == "sheets.googleapis.com":
            if "supportsAllDrives" in request.url.params:      # the real Sheets API refuses unknown parameters
                return _error(400, "badRequest")
            return self._sheets(request, path)
        if request.url.params.get("supportsAllDrives") != "true":
            return _error(404, "notFound")                      # shared-drive items are invisible without it
        if path.startswith("/upload/drive/v3/files"):
            return self._upload(request)
        return self._drive(request, path)

    def _token(self, request: httpx.Request) -> httpx.Response:
        form = dict(urllib.parse.parse_qsl(request.content.decode()))
        try:
            claims = jwt.decode(form["assertion"], public_pem(), algorithms=["RS256"], audience=TOKEN_URI)
        except Exception:  # noqa: BLE001
            return httpx.Response(400, json={"error": "invalid_grant"})
        if self.refuse_token or form.get("grant_type") != "urn:ietf:params:oauth:grant-type:jwt-bearer":
            return httpx.Response(400, json={"error": "invalid_grant"})
        self.claims = claims
        self.token_requests += 1
        return httpx.Response(200, json={"access_token": f"tok{self.token_requests}", "expires_in": 3600})

    def _drive(self, request: httpx.Request, path: str) -> httpx.Response:
        parts = path[len("/drive/v3/files"):].strip("/").split("/") if path != "/drive/v3/files" else []
        body = json.loads(request.content) if request.content else {}
        if not parts:
            return self._list(request) if request.method == "GET" else self._create(body)
        file_id = parts[0]
        if file_id not in self.files or self.files[file_id]["trashed"] and request.method != "DELETE":
            return _error(404, "notFound")
        if len(parts) == 1:
            if request.method == "PATCH":
                self.files[file_id].update({k: body[k] for k in ("name", "trashed") if k in body})
                return httpx.Response(200, json={"id": file_id})
            if request.method == "DELETE":
                if not self.organizer:
                    return _error(403, "insufficientFilePermissions")
                self._remove(file_id)
                return httpx.Response(204)
        perms = self.permissions.setdefault(file_id, [])
        if len(parts) == 2 and request.method == "GET":
            return httpx.Response(200, json={"permissions": perms})
        if len(parts) == 2 and request.method == "POST":
            self.permission_requests.append(body)
            if body.get("emailAddress") in self.refused_emails:
                return _error(400, "invalidSharingRequest")
            perm = {"id": self.new_id("perm"), "type": body["type"], "role": body["role"],
                    "emailAddress": body.get("emailAddress")}
            perms.append(perm)
            return httpx.Response(200, json={"id": perm["id"]})
        if len(parts) == 3 and request.method == "DELETE":
            if not any(p["id"] == parts[2] for p in perms):
                return _error(404, "notFound")
            self.permissions[file_id] = [p for p in perms if p["id"] != parts[2]]
            return httpx.Response(204)
        return _error(400, "badRequest")

    def _list(self, request: httpx.Request) -> httpx.Response:
        params = request.url.params
        if (params.get("corpora"), params.get("driveId"), params.get("includeItemsFromAllDrives")) != ("drive", DRIVE_ID, "true"):
            return _error(400, "badRequest")
        q = params["q"]
        parent = re.search(r"'((?:[^'\\]|\\.)*)' in parents", q)
        name = re.search(r"name = '((?:[^'\\]|\\.)*)'", q)
        mime = re.search(r"mimeType = '([^']*)'", q)
        found = [{"id": i, "name": f["name"], "mimeType": f["mimeType"]} for i, f in self.files.items()
                 if not f["trashed"] and _unescape(parent.group(1)) in f["parents"]
                 and (name is None or f["name"] == _unescape(name.group(1)))
                 and (mime is None or f["mimeType"] == mime.group(1))]
        return httpx.Response(200, json={"files": found})

    def _create(self, body: Dict[str, Any], content: Optional[bytes] = None) -> httpx.Response:
        parent = body["parents"][0]
        if parent not in self.files:
            return _error(404, "notFound")
        mime = body.get("mimeType") or "application/octet-stream"
        file_id = self.new_id({FOLDER_MIME: "folder", SHEET_MIME: "sheet"}.get(mime, "file"))
        self.files[file_id] = {"name": body["name"], "mimeType": mime, "parents": [parent], "trashed": False,
                               "content": content}
        self.created.append((body["name"], mime))
        if mime == SHEET_MIME:
            self.sheets[file_id] = []
        return httpx.Response(200, json={"id": file_id, "webViewLink": f"https://drive.google.com/file/d/{file_id}/view"})

    def _remove(self, file_id: str) -> None:
        for child in [i for i, f in self.files.items() if file_id in f["parents"]]:
            self._remove(child)
        self.files.pop(file_id, None)
        self.sheets.pop(file_id, None)
        self.deleted.append(file_id)

    def _upload(self, request: httpx.Request) -> httpx.Response:
        params = request.url.params
        if request.method == "POST" and params.get("uploadType") == "resumable":
            session = self.new_id("upload")
            self.sessions[session] = {"meta": json.loads(request.content), "data": bytearray(),
                                      "total": int(request.headers["x-upload-content-length"])}
            location = f"https://www.googleapis.com/upload/drive/v3/files?uploadType=resumable&upload_id={session}"
            return httpx.Response(200, headers={"Location": location})
        state = self.sessions.get(params.get("upload_id", ""))
        if request.method != "PUT" or state is None:
            return _error(404, "notFound")
        match = re.fullmatch(r"bytes (\d+)-(\d+)/(\d+)", request.headers.get("content-range", ""))
        if match is None or int(match.group(1)) != len(state["data"]):
            return _error(400, "badContentRange")
        state["data"] += request.content
        self.chunks.append(len(request.content))
        if len(state["data"]) < state["total"]:
            return httpx.Response(308, headers={"Range": f"bytes=0-{len(state['data']) - 1}"})
        return self._create(state["meta"], bytes(state["data"]))

    def _sheets(self, request: httpx.Request, path: str) -> httpx.Response:
        match = re.fullmatch(r"/v4/spreadsheets/([^/]+)/values/(.+)", path)
        if match is None or match.group(1) not in self.sheets:
            return _error(404, "notFound")
        rows = self.sheets[match.group(1)]
        target = match.group(2)
        if request.method == "GET":
            values = [[r[0]] for r in rows] if target == "A:A" else [list(r) for r in rows]
            return httpx.Response(200, json={"values": values} if values else {})
        if target.endswith(":append"):
            rows.extend(json.loads(request.content)["values"])
            return httpx.Response(200, json={})
        if target.endswith(":clear"):
            rows.clear()
            return httpx.Response(200, json={})
        return _error(400, "badRequest")


class GoogleFakeMixin:
    """Point google_drive at a FakeGoogle with a throw-away service-account key."""

    def install_google(self, delivery: bool = True, **extra_settings: Any) -> FakeGoogle:
        self.google = FakeGoogle()
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp_dir = Path(tmp.name)
        self.key_file = self.tmp_dir / "service-account.json"
        self.key_file.write_text(json.dumps({"type": "service_account", "client_email": SA_EMAIL,
                                             "private_key": private_pem(), "token_uri": TOKEN_URI}), encoding="utf-8")
        for p in (patch.multiple(settings, GOOGLE_SA_KEY_FILE=str(self.key_file), DRIVE_SHARED_DRIVE_ID=DRIVE_ID,
                                 DRIVE_DELIVERY_ENABLED=delivery, **extra_settings),
                  patch.object(google_drive, "_transport", httpx.MockTransport(self.google.handler)),
                  patch.object(google_drive, "BACKOFF_SECONDS", 0.0)):
            p.start()
            self.addCleanup(p.stop)
        google_drive.reset_for_tests()
        self.addCleanup(google_drive.reset_for_tests)
        return self.google


class DriveDbTestBase(GoogleFakeMixin, unittest.TestCase):
    """A real database (several connections: the code under test opens its own sessions) plus the fake Google."""

    def setUp(self) -> None:
        self.engine = make_engine(concurrent=True)
        Base.metadata.create_all(bind=self.engine)
        self.Session = sessionmaker(bind=self.engine)
        self.addCleanup(self.engine.dispose)
        p = patch("app.database.SessionLocal", self.Session)
        p.start()
        self.addCleanup(p.stop)
        self.install_google()

    def make_customer(self, email: Optional[str] = None, phone: str = PHONE) -> str:
        with self.Session() as db:
            customer = Customer(whatsapp_id=phone, full_name="T", business_name="B", gst_number="N/A", address="A",
                                email=email)
            db.add(customer)
            db.commit()
            return customer.id

    def customer(self, customer_id: str) -> Customer:
        with self.Session() as db:
            return db.get(Customer, customer_id)

    def set_email(self, customer_id: str, email: Optional[str]) -> None:
        with self.Session() as db:
            db.get(Customer, customer_id).email = email
            db.commit()

    def image(self, name: str = "white.png", size: int = 1000) -> str:
        path = self.tmp_dir / name
        path.write_bytes(os.urandom(size))
        return str(path)


# ── google_drive ───────────────────────────────────────────────────────────────────────────────────────────

class ConfiguredTests(GoogleFakeMixin, unittest.TestCase):
    def test_needs_a_readable_key_and_the_shared_drive(self):
        self.install_google()
        self.assertTrue(google_drive.configured())
        with patch.object(settings, "DRIVE_SHARED_DRIVE_ID", ""):
            self.assertFalse(google_drive.configured())
        for content in ("not json", json.dumps({"client_email": SA_EMAIL, "token_uri": TOKEN_URI}), "[]"):
            google_drive.reset_for_tests()
            self.key_file.write_text(content, encoding="utf-8")
            self.assertFalse(google_drive.configured())
        google_drive.reset_for_tests()
        with patch.object(settings, "GOOGLE_SA_KEY_FILE", str(self.tmp_dir / "missing.json")):
            self.assertFalse(google_drive.configured())

    def test_nothing_is_configured_by_default(self):
        self.assertFalse(google_drive.configured())
        self.assertFalse(drive_layout.delivery_enabled())


class ClientTests(GoogleFakeMixin, unittest.TestCase):
    def setUp(self) -> None:
        self.install_google()

    def test_the_token_is_a_signed_service_account_jwt_and_is_cached(self):
        async def calls():
            await google_drive.create_folder("a", DRIVE_ID)
            await google_drive.list_children(DRIVE_ID)
            await asyncio.gather(*[google_drive.find_child("a", DRIVE_ID) for _ in range(5)])

        asyncio.run(calls())
        self.assertEqual(self.google.token_requests, 1)
        self.assertEqual(self.google.claims["iss"], SA_EMAIL)
        self.assertEqual(self.google.claims["scope"],
                         "https://www.googleapis.com/auth/drive https://www.googleapis.com/auth/spreadsheets")
        self.assertEqual(self.google.claims["exp"] - self.google.claims["iat"], 3600)

    def test_concurrent_first_calls_fetch_one_token(self):
        async def calls():
            await asyncio.gather(*[google_drive.list_children(DRIVE_ID) for _ in range(6)])

        asyncio.run(calls())
        self.assertEqual(self.google.token_requests, 1)

    def test_an_expired_token_is_replaced_after_a_401(self):
        asyncio.run(google_drive.list_children(DRIVE_ID))
        self.google.fail("GET", "/drive/v3/files", 401, "authError")
        asyncio.run(google_drive.list_children(DRIVE_ID))
        self.assertEqual(self.google.token_requests, 2)

    def test_a_429_is_retried_and_then_succeeds(self):
        self.google.fail("POST", "/drive/v3/files", 429, "rateLimitExceeded")
        folder = asyncio.run(google_drive.create_folder("Images", DRIVE_ID))
        self.assertEqual(self.google.files[folder]["name"], "Images")
        self.assertEqual(self.google.count("POST", "/drive/v3/files"), 2)

    def test_a_403_rate_limit_is_retried_but_another_403_is_not(self):
        self.google.fail("POST", "/drive/v3/files", 403, "userRateLimitExceeded")
        asyncio.run(google_drive.create_folder("x", DRIVE_ID))
        self.assertEqual(self.google.count("POST", "/drive/v3/files"), 2)
        self.google.fail("POST", "/drive/v3/files", 403, "insufficientFilePermissions")
        with self.assertRaises(DriveError) as ctx:
            asyncio.run(google_drive.create_folder("y", DRIVE_ID))
        self.assertEqual((ctx.exception.status, ctx.exception.reason, ctx.exception.retryable),
                         (403, "insufficientFilePermissions", False))

    def test_server_errors_are_retried_twice_then_left_to_the_outbox(self):
        self.google.fail("POST", "/drive/v3/files", 503, times=3)
        with self.assertRaises(DriveError) as ctx:
            asyncio.run(google_drive.create_folder("x", DRIVE_ID))
        self.assertTrue(ctx.exception.retryable)
        self.assertEqual(self.google.count("POST", "/drive/v3/files"), 3)

    def test_a_400_is_not_retried(self):
        self.google.fail("POST", "/drive/v3/files", 400, "invalid")
        with self.assertRaises(DriveError) as ctx:
            asyncio.run(google_drive.create_folder("x", DRIVE_ID))
        self.assertEqual((ctx.exception.status, ctx.exception.reason, ctx.exception.retryable), (400, "invalid", False))
        self.assertEqual(self.google.count("POST", "/drive/v3/files"), 1)

    def test_a_refused_key_is_retryable_and_never_leaks_the_key(self):
        self.google.refuse_token = True
        with self.assertRaises(DriveError) as ctx:
            asyncio.run(google_drive.list_children(DRIVE_ID))
        self.assertTrue(ctx.exception.retryable)
        self.assertNotIn("PRIVATE KEY", str(ctx.exception))
        self.assertEqual(self.google.calls, [])

    def test_resumable_upload_streams_the_file_in_chunks(self):
        data = os.urandom(600_000)
        path = self.tmp_dir / "big.png"
        path.write_bytes(data)
        chunk = 256 * 1024
        with patch.object(google_drive, "CHUNK_SIZE", chunk), \
                patch.object(google_drive, "_read_chunk", wraps=google_drive._read_chunk) as reader, \
                patch.object(Path, "read_bytes", side_effect=AssertionError("whole file read")):
            uploaded = asyncio.run(google_drive.upload_file(str(path), "001.png", DRIVE_ID, "image/png"))
        self.assertEqual(self.google.files[uploaded["id"]]["content"], data)
        self.assertEqual(self.google.files[uploaded["id"]]["name"], "001.png")
        self.assertEqual(self.google.chunks, [chunk, chunk, 600_000 - 2 * chunk])
        self.assertEqual([c.args[1:] for c in reader.call_args_list], [(0, chunk), (chunk, chunk), (2 * chunk, chunk)])
        self.assertEqual(uploaded["link"], f"https://drive.google.com/file/d/{uploaded['id']}/view")

    def test_a_chunk_that_fails_once_is_sent_again(self):
        path = self.tmp_dir / "a.png"
        path.write_bytes(b"x" * 300_000)
        self.google.fail("PUT", "/upload/drive/v3/files", 503)
        with patch.object(google_drive, "CHUNK_SIZE", 256 * 1024):
            uploaded = asyncio.run(google_drive.upload_file(str(path), "a.png", DRIVE_ID, "image/png"))
        self.assertEqual(self.google.files[uploaded["id"]]["content"], b"x" * 300_000)

    def test_find_child_escapes_quotes_and_filters_by_type(self):
        async def go():
            folder = await google_drive.create_folder("O'Brien \\ Co", DRIVE_ID)
            sheet = await google_drive.create_sheet("O'Brien \\ Co", DRIVE_ID)
            return folder, sheet, await google_drive.find_child("O'Brien \\ Co", DRIVE_ID, FOLDER_MIME), \
                await google_drive.find_child("O'Brien \\ Co", DRIVE_ID, SHEET_MIME), \
                await google_drive.find_child("nobody", DRIVE_ID)

        folder, sheet, found_folder, found_sheet, missing = asyncio.run(go())
        self.assertEqual((found_folder, found_sheet, missing), (folder, sheet, None))

    def test_delete_counts_a_missing_file_as_done_and_trashes_when_permanent_delete_is_refused(self):
        async def go():
            await google_drive.delete("does-not-exist")
            folder = await google_drive.create_folder("x", DRIVE_ID)
            self.google.organizer = False
            await google_drive.delete(folder)
            return folder

        folder = asyncio.run(go())
        self.assertTrue(self.google.files[folder]["trashed"])

    def test_share_only_takes_a_user_email_and_a_known_role(self):
        with self.assertRaises(ValueError):
            asyncio.run(google_drive.share_with_email(DRIVE_ID, "a@b.com", role="owner"))
        with self.assertRaises(ValueError):
            asyncio.run(google_drive.share_with_email(DRIVE_ID, "not-an-email"))
        self.assertEqual(self.google.permission_requests, [])

    def test_delete_files_sync_deletes_and_counts_already_gone(self):
        async def make():
            return [await google_drive.create_folder(n, DRIVE_ID) for n in ("a", "b")]

        a, b = asyncio.run(make())
        self.assertEqual(google_drive.delete_files_sync([a, None, "gone", b]), 3)
        self.assertNotIn(a, self.google.files)
        self.assertNotIn(b, self.google.files)

    def test_delete_files_sync_does_nothing_when_not_configured(self):
        with patch.object(settings, "DRIVE_SHARED_DRIVE_ID", ""):
            self.assertEqual(google_drive.delete_files_sync(["a"]), 0)
        self.assertEqual(self.google.calls, [])


# ── drive_layout ───────────────────────────────────────────────────────────────────────────────────────────

class CustomerFolderTests(DriveDbTestBase):
    def test_four_concurrent_workers_create_exactly_one_folder_set(self):
        customer_id = self.make_customer()

        async def four():
            return await asyncio.gather(*[drive_layout.ensure_customer_folders(customer_id) for _ in range(4)])

        results = asyncio.run(four())
        self.assertEqual(len({tuple(sorted(r.items())) for r in results}), 1)
        self.assertEqual(self.google.created, [(PHONE, FOLDER_MIME), ("Images", FOLDER_MIME), ("Invoices", FOLDER_MIME),
                                               ("Usage Logs", SHEET_MIME)])
        ids = results[0]
        row = self.customer(customer_id)
        self.assertEqual((row.drive_folder_id, row.drive_images_folder_id, row.drive_invoices_folder_id, row.drive_sheet_id),
                         (ids["root"], ids["images"], ids["invoices"], ids["sheet"]))
        self.assertEqual(self.google.files[ids["root"]]["parents"], [DRIVE_ID])
        self.assertEqual(self.google.sheets[ids["sheet"]], [drive_layout.USAGE_LOG_HEADER])
        before = len(self.google.calls)
        self.assertEqual(asyncio.run(drive_layout.ensure_customer_folders(customer_id)), ids)
        self.assertEqual(len(self.google.calls), before)                 # stored ids: no Drive call at all

    def test_separate_processes_racing_keep_one_set_and_delete_their_extra_copies(self):
        customer_id = self.make_customer()

        async def four():
            return await asyncio.gather(*[drive_layout.ensure_customer_folders(customer_id) for _ in range(4)])

        with patch.object(drive_layout, "customer_lock", side_effect=lambda _id: asyncio.Lock()):   # no shared lock
            results = asyncio.run(four())
        self.assertEqual(len({tuple(sorted(r.items())) for r in results}), 1)
        self.assertEqual(self.google.children(DRIVE_ID), [results[0]["root"]])
        self.assertEqual(self.customer(customer_id).drive_folder_id, results[0]["root"])

    def test_a_failure_half_way_removes_the_new_root_and_stores_nothing(self):
        customer_id = self.make_customer()
        with patch.object(google_drive, "create_sheet", AsyncMock(side_effect=DriveError(503, "backendError", True))):
            with self.assertRaises(DriveError):
                asyncio.run(drive_layout.ensure_customer_folders(customer_id))
        self.assertEqual(self.google.children(DRIVE_ID), [])
        self.assertIsNone(self.customer(customer_id).drive_folder_id)

    def test_day_folders_use_the_ist_date_and_files_are_numbered(self):
        customer_id = self.make_customer()
        when = datetime(2026, 10, 6, 20, 0, tzinfo=timezone.utc)        # 7 Oct 2026, 01:30 in India
        self.assertEqual(drive_layout.day_label(when), "7 Oct 2026")
        self.assertEqual(drive_layout.day_label(datetime(2026, 1, 5, 6, 0)), "5 Jan 2026")

        async def uploads():
            first = await drive_layout.upload_delivery_image(customer_id, self.image("a.png"), "image/png", when)
            more = await asyncio.gather(
                drive_layout.upload_delivery_image(customer_id, self.image("b.png"), "image/png", when),
                drive_layout.upload_delivery_image(customer_id, self.image("c.jpg"), "image/jpeg", when),
            )
            next_day = await drive_layout.upload_delivery_image(customer_id, self.image("d.png"), "image/png",
                                                                when + timedelta(days=1))
            return first, more, next_day, await drive_layout.ensure_day_folder(customer_id, when)

        first, more, next_day, day = asyncio.run(uploads())
        self.assertEqual(first["name"], "001.png")
        self.assertEqual(sorted(u["name"] for u in more), ["002.png", "003.jpg"])
        self.assertEqual(day, (first["day_folder_id"], "7 Oct 2026"))
        self.assertEqual({u["day_folder_id"] for u in more}, {first["day_folder_id"]})
        self.assertEqual(first["day_folder_link"], f"https://drive.google.com/drive/folders/{first['day_folder_id']}")
        images = self.customer(customer_id).drive_images_folder_id
        self.assertEqual(self.google.files[first["day_folder_id"]]["parents"], [images])
        self.assertEqual((next_day["name"], self.google.files[next_day["day_folder_id"]]["name"]), ("001.png", "8 Oct 2026"))
        self.assertEqual(len([n for n, m in self.google.created if m == FOLDER_MIME and n == "7 Oct 2026"]), 1)

    def test_an_invoice_goes_into_invoices(self):
        customer_id = self.make_customer()
        pdf = self.image("inv.pdf")
        uploaded = asyncio.run(drive_layout.upload_invoice(customer_id, pdf, "2026-10-07 INV-0001.pdf"))
        stored = self.google.files[uploaded["file_id"]]
        self.assertEqual((stored["name"], stored["mimeType"], stored["parents"]),
                         ("2026-10-07 INV-0001.pdf", "application/pdf", [self.customer(customer_id).drive_invoices_folder_id]))
        self.assertTrue(uploaded["link"].startswith("https://drive.google.com/"))


class ShareTests(DriveDbTestBase):
    def setUp(self) -> None:
        super().setUp()
        self.sent = AsyncMock(return_value=True)
        p = patch("app.services.meta_whatsapp_service.send_whatsapp_text", self.sent)
        p.start()
        self.addCleanup(p.stop)

    def test_the_root_is_shared_with_the_customer_email_as_reader(self):
        customer_id = self.make_customer(email="buyer@gmail.com")
        self.assertEqual(asyncio.run(drive_layout.share_customer_folder(customer_id)), "shared")
        root = self.customer(customer_id).drive_folder_id
        self.assertEqual([(p["type"], p["role"], p["emailAddress"]) for p in self.google.permissions[root]],
                         [("user", "reader", "buyer@gmail.com")])
        self.assertEqual(self.customer(customer_id).drive_shared_to, "buyer@gmail.com")
        self.assertEqual(asyncio.run(drive_layout.share_customer_folder(customer_id)), "already")
        self.assertEqual(len(self.google.permission_requests), 1)
        self.sent.assert_not_called()

    def test_a_new_email_replaces_the_old_one(self):
        customer_id = self.make_customer(email="old@gmail.com")
        asyncio.run(drive_layout.share_customer_folder(customer_id))
        self.set_email(customer_id, "new@gmail.com")
        self.assertEqual(asyncio.run(drive_layout.share_customer_folder(customer_id)), "shared")
        root = self.customer(customer_id).drive_folder_id
        self.assertEqual([p["emailAddress"] for p in self.google.permissions[root]], ["new@gmail.com"])
        self.assertEqual(self.customer(customer_id).drive_shared_to, "new@gmail.com")

    def test_a_refused_address_keeps_the_folder_private_and_asks_once_for_a_google_email(self):
        from app.utils.logger import logger

        customer_id = self.make_customer(email="someone@company.example")
        self.google.refused_emails.add("someone@company.example")
        before = metrics.registry.counter_value("moraa_drive_share_total", {"outcome": "refused"})
        lines: List[str] = []
        sink = logger.add(lambda m: lines.append(m.record["message"]), level="DEBUG")
        try:
            outcome = asyncio.run(drive_layout.share_customer_folder(customer_id))
        finally:
            logger.remove(sink)
        self.assertEqual(outcome, "refused")
        root = self.customer(customer_id).drive_folder_id
        self.assertEqual(self.google.permissions.get(root, []), [])
        self.assertEqual({p["type"] for p in self.google.permission_requests}, {"user"})
        self.sent.assert_awaited_once_with(PHONE, drive_layout.GOOGLE_EMAIL_REQUEST)
        self.assertIsNone(self.customer(customer_id).drive_shared_to)
        self.assertEqual(metrics.registry.counter_value("moraa_drive_share_total", {"outcome": "refused"}), before + 1)
        alerts = [line for line in lines if line.startswith("ALERT drive share refused")]
        self.assertEqual(len(alerts), 1)
        self.assertNotIn("someone@company.example", "\n".join(lines))
        self.assertNotIn(PHONE, "\n".join(lines))
        # the outbox job is done (no retry), and nothing ever asks for a public link
        self.assertTrue(asyncio.run(drive_layout.handle_share_job({"customer_id": customer_id})))
        self.assertNotIn("anyone", json.dumps(self.google.permission_requests))

    def test_no_email_means_nothing_to_share_yet(self):
        customer_id = self.make_customer()
        self.assertEqual(asyncio.run(drive_layout.share_customer_folder(customer_id)), "no_email")
        self.assertEqual(self.google.calls, [])

    def test_not_configured_shares_nothing_and_queues_nothing(self):
        customer_id = self.make_customer(email="buyer@gmail.com")
        with patch.object(settings, "DRIVE_DELIVERY_ENABLED", False):
            self.assertEqual(asyncio.run(drive_layout.share_customer_folder(customer_id)), "not_configured")
            drive_layout.queue_share(customer_id)
        with patch.object(settings, "DRIVE_SHARED_DRIVE_ID", ""):
            drive_layout.queue_share(customer_id)
        with self.Session() as db:
            self.assertEqual(db.query(OutboxJob).count(), 0)
        self.assertEqual(self.google.calls, [])

    def test_queue_share_records_one_job_per_minute_and_never_the_email(self):
        customer_id = self.make_customer(email="buyer@gmail.com")
        with patch.object(drive_layout.time, "time", return_value=60_000.0):
            drive_layout.queue_share(customer_id)
            drive_layout.queue_share(customer_id)
        self.set_email(customer_id, "buyer@gmail.com")                 # back to an earlier address later: shared again
        with patch.object(drive_layout.time, "time", return_value=60_120.0):
            drive_layout.queue_share(customer_id)
        with self.Session() as db:
            jobs = db.query(OutboxJob).order_by(OutboxJob.id).all()
            self.assertEqual([(j.kind, j.payload, j.dedupe_key) for j in jobs], [
                ("drive_share", {"customer_id": customer_id}, f"share:{customer_id}:1000"),
                ("drive_share", {"customer_id": customer_id}, f"share:{customer_id}:1002"),
            ])
            self.assertFalse(any("@" in j.dedupe_key for j in jobs))

    def test_the_outbox_job_raises_on_a_retryable_failure(self):
        customer_id = self.make_customer(email="buyer@gmail.com")
        self.google.fail("POST", "/permissions", 503, times=3)
        with self.assertRaises(DriveError):
            asyncio.run(drive_layout.handle_share_job({"customer_id": customer_id}))
        self.assertTrue(asyncio.run(drive_layout.handle_share_job({"customer_id": customer_id})))
        self.assertEqual(self.customer(customer_id).drive_shared_to, "buyer@gmail.com")


class ErasureTests(DriveDbTestBase):
    def test_erasure_removes_images_and_usage_log_unshares_renames_and_keeps_invoices(self):
        customer_id = self.make_customer(email="buyer@gmail.com")
        with patch("app.services.meta_whatsapp_service.send_whatsapp_text", AsyncMock(return_value=True)):
            asyncio.run(drive_layout.share_customer_folder(customer_id))
        image = asyncio.run(drive_layout.upload_delivery_image(customer_id, self.image(), "image/png"))
        invoice = asyncio.run(drive_layout.upload_invoice(customer_id, self.image("i.pdf"), "2026-10-07 INV-1.pdf"))
        before = self.customer(customer_id)
        removed = asyncio.run(drive_layout.erase_customer_drive(customer_id))
        self.assertEqual(removed, {"images": 1, "sheet": 1, "permissions": 1, "renamed": 1})
        for gone in (before.drive_images_folder_id, before.drive_sheet_id, image["day_folder_id"], image["file_id"]):
            self.assertNotIn(gone, self.google.files)
        self.assertEqual(self.google.permissions[before.drive_folder_id], [])
        self.assertEqual(self.google.files[before.drive_folder_id]["name"], f"erased-{customer_id[:8]}")
        self.assertIn(invoice["file_id"], self.google.children(before.drive_invoices_folder_id))
        after = self.customer(customer_id)
        self.assertEqual((after.drive_folder_id, after.drive_invoices_folder_id), (before.drive_folder_id,
                                                                                   before.drive_invoices_folder_id))
        self.assertEqual((after.drive_images_folder_id, after.drive_sheet_id, after.drive_shared_to), (None, None, None))
        with self.assertRaises(DriveError):                       # nothing is ever delivered into an erased set
            asyncio.run(drive_layout.ensure_customer_folders(customer_id))

    def test_a_customer_without_folders_has_nothing_to_erase(self):
        customer_id = self.make_customer()
        self.assertEqual(asyncio.run(drive_layout.erase_customer_drive(customer_id)),
                         {"images": 0, "sheet": 0, "permissions": 0, "renamed": 0})
        self.assertEqual(self.google.calls, [])

    def test_retention_deletes_delivered_images(self):
        customer_id = self.make_customer()
        image = asyncio.run(drive_layout.upload_delivery_image(customer_id, self.image(), "image/png"))
        self.assertEqual(drive_layout.delete_drive_files_sync([image["file_id"], None]), 1)
        self.assertNotIn(image["file_id"], self.google.files)


if __name__ == "__main__":
    unittest.main()
