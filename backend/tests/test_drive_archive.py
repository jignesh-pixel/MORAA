"""The Google Drive archive of photos and produced images."""

import asyncio
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import httpx

from app.config import settings
from app.models.chat_log import ChatMessage, OrderOutput
from app.services import drive_archive
from tests.test_wallet_ledger import SENDER, LedgerTestBase

CFG = dict(DRIVE_ENABLED=True, GOOGLE_DRIVE_CLIENT_ID="cid", GOOGLE_DRIVE_CLIENT_SECRET="csecret",
           GOOGLE_DRIVE_REFRESH_TOKEN="rtoken", DRIVE_FOLDER_ID="folder1")


class _Drive:
    def __init__(self, upload_status=200):
        self.calls = []
        self.upload_status = upload_status

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.calls.append((request.method, str(request.url).split("?")[0], request.headers.get("authorization"), request.content))
        if "oauth2.googleapis.com/token" in str(request.url):
            return httpx.Response(200, json={"access_token": "tok1", "expires_in": 3600})
        if "upload/drive/v3/files" in str(request.url):
            if self.upload_status != 200:
                return httpx.Response(self.upload_status, json={})
            return httpx.Response(200, json={"id": "fileX", "webViewLink": "https://drive.google.com/file/d/fileX/view"})
        return httpx.Response(404)


def _with_drive(drive, coro_factory):
    real = httpx.AsyncClient

    def factory(*a, **k):
        k["transport"] = httpx.MockTransport(drive.handler)
        return real(*a, **k)

    with patch.multiple(settings, **CFG), patch.object(httpx, "AsyncClient", side_effect=factory):
        return asyncio.run(coro_factory())


class _Base(LedgerTestBase):
    def setUp(self):
        super().setUp()
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        for p in (patch("app.database.SessionLocal", return_value=self.db), patch.object(self.db, "close"),
                  patch.object(type(settings), "UPLOAD_PATH", new=property(lambda s: Path(self.tmp.name)))):
            p.start()
            self.addCleanup(p.stop)
        drive_archive.reset_for_tests()
        path = Path(self.tmp.name) / "outputs" / "i1" / "a.png"
        path.parent.mkdir(parents=True)
        path.write_bytes(b"PNGBYTES")
        self.output = OrderOutput(ingestion_id="i1", style="Style A", position=1, file_path="outputs/i1/a.png", mime_type="image/png")
        self.db.add(self.output)
        self.db.commit()


class ArchiveTests(_Base):
    def test_an_output_is_uploaded_with_the_folder_and_the_link_is_saved(self):
        drive = _Drive()
        self.assertTrue(_with_drive(drive, lambda: drive_archive.archive("output", self.output.id)))
        upload = [c for c in drive.calls if "upload" in c[1]][0]
        self.assertEqual(upload[2], "Bearer tok1")
        self.assertIn(b'"parents": ["folder1"]', upload[3])
        self.assertIn(b"PNGBYTES", upload[3])
        self.db.expire_all()
        row = self.db.get(OrderOutput, self.output.id)
        self.assertEqual((row.drive_file_id, row.drive_link), ("fileX", "https://drive.google.com/file/d/fileX/view"))

    def test_an_already_archived_file_is_not_uploaded_again(self):
        self.output.drive_file_id = "old"
        self.db.commit()
        drive = _Drive()
        self.assertTrue(_with_drive(drive, lambda: drive_archive.archive("output", self.output.id)))
        self.assertEqual([c for c in drive.calls if "upload" in c[1]], [])

    def test_a_drive_failure_raises_so_the_outbox_retries_and_nothing_is_marked_done(self):
        drive = _Drive(upload_status=500)
        with self.assertRaises(RuntimeError):
            _with_drive(drive, lambda: drive_archive.archive("output", self.output.id))
        self.db.expire_all()
        self.assertIsNone(self.db.get(OrderOutput, self.output.id).drive_file_id)

    def test_refused_credentials_fail_without_leaking_them(self):
        def handler(request):
            return httpx.Response(401, json={"error": "invalid_grant"})

        drive = _Drive()
        drive.handler = handler
        with self.assertRaises(RuntimeError) as ctx:
            _with_drive(drive, lambda: drive_archive.archive("output", self.output.id))
        self.assertNotIn("rtoken", str(ctx.exception))

    def test_it_does_nothing_unless_configured(self):
        self.assertFalse(drive_archive.configured())
        self.assertTrue(asyncio.run(drive_archive.archive("output", self.output.id)))

    def test_an_inbound_photo_message_is_archived_too(self):
        from app.models.image import Image

        img_path = Path(self.tmp.name) / "req1" / "orig.jpg"
        img_path.parent.mkdir(parents=True)
        img_path.write_bytes(b"JPEG")
        img = Image(request_id="req1", original_filename="o.jpg", stored_filename="orig.jpg", file_path=str(img_path),
                    file_size=4, mime_type="image/jpeg", image_url="/u", processing_status="stored")
        self.db.add(img)
        self.db.commit()
        msg = ChatMessage(customer_phone=SENDER, direction="in", msg_type="image", image_id=img.id, ingestion_id="i1")
        self.db.add(msg)
        self.db.commit()
        drive = _Drive()
        self.assertTrue(_with_drive(drive, lambda: drive_archive.archive("photo", msg.id)))
        self.db.expire_all()
        self.assertEqual(self.db.get(ChatMessage, msg.id).drive_file_id, "fileX")

    def test_the_token_is_reused_between_uploads(self):
        drive = _Drive()

        async def twice():
            await drive_archive.upload_file("a.png", b"1", "image/png")
            await drive_archive.upload_file("b.png", b"2", "image/png")

        _with_drive(drive, twice)
        self.assertEqual(len([c for c in drive.calls if "oauth2" in c[1]]), 1)


class DeleteTests(unittest.TestCase):
    def test_copies_are_deleted_and_an_already_gone_copy_counts(self):
        seen = []

        def handler(request):
            if "oauth2" in str(request.url):
                return httpx.Response(200, json={"access_token": "t"})
            seen.append((request.method, str(request.url).split("?")[0]))
            return httpx.Response(404 if str(request.url).endswith("gone") else 204)

        real = httpx.Client

        def factory(*a, **k):
            k["transport"] = httpx.MockTransport(handler)
            return real(*a, **k)

        with patch.multiple(settings, **CFG), patch.object(httpx, "Client", side_effect=factory):
            self.assertEqual(drive_archive.delete_files_sync(["a1", None, "gone"]), 2)
        self.assertEqual(seen, [("DELETE", "https://www.googleapis.com/drive/v3/files/a1"),
                                ("DELETE", "https://www.googleapis.com/drive/v3/files/gone")])

    def test_nothing_happens_when_drive_is_off(self):
        self.assertEqual(drive_archive.delete_files_sync(["a1"]), 0)


if __name__ == "__main__":
    unittest.main()
