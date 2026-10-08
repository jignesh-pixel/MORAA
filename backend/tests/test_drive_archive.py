"""The Google Drive archive of photos and produced images, on the business service account (Phase 8)."""

import asyncio
import unittest
from pathlib import Path
from unittest.mock import patch

from app.config import settings
from app.models.chat_log import ChatMessage, OrderOutput
from app.services import drive_archive
from app.services.google_drive import DriveError
from tests.test_google_drive import DRIVE_ID, GoogleFakeMixin
from tests.test_wallet_ledger import SENDER, LedgerTestBase


class _Base(GoogleFakeMixin, LedgerTestBase):
    def setUp(self):
        super().setUp()
        self.google = self.install_google(delivery=False, DRIVE_ENABLED=True, DRIVE_FOLDER_ID="")
        self.folder = self.google.add_folder("Archive")
        for p in (patch.object(settings, "DRIVE_FOLDER_ID", self.folder),
                  patch("app.database.SessionLocal", return_value=self.db), patch.object(self.db, "close"),
                  patch.object(type(settings), "UPLOAD_PATH", new=property(lambda s: self.tmp_dir))):
            p.start()
            self.addCleanup(p.stop)
        path = self.tmp_dir / "outputs" / "i1" / "a.png"
        path.parent.mkdir(parents=True)
        path.write_bytes(b"PNGBYTES")
        self.output = OrderOutput(ingestion_id="i1", style="Style A", position=1, file_path="outputs/i1/a.png",
                                  mime_type="image/png")
        self.db.add(self.output)
        self.db.commit()

    def uploaded(self):
        return [(f["name"], f["parents"]) for f in self.google.files.values() if f["mimeType"] != "application/vnd.google-apps.folder"]


class ArchiveTests(_Base):
    def test_an_output_is_uploaded_into_the_archive_folder_and_the_link_is_saved(self):
        self.assertTrue(asyncio.run(drive_archive.archive("output", self.output.id)))
        [(name, parents)] = self.uploaded()
        self.assertEqual(parents, [self.folder])
        self.assertTrue(name.startswith("i1_Style_A_") and name.endswith(".png"))
        self.db.expire_all()
        row = self.db.get(OrderOutput, self.output.id)
        self.assertTrue(row.drive_file_id)
        self.assertIn(row.drive_file_id, row.drive_link)

    def test_without_a_folder_the_file_goes_to_the_shared_drive(self):
        with patch.object(settings, "DRIVE_FOLDER_ID", ""):
            asyncio.run(drive_archive.archive("output", self.output.id))
        self.assertEqual(self.uploaded()[0][1], [DRIVE_ID])

    def test_an_already_archived_file_is_not_uploaded_again(self):
        self.output.drive_file_id = "old"
        self.db.commit()
        self.assertTrue(asyncio.run(drive_archive.archive("output", self.output.id)))
        self.assertEqual(self.uploaded(), [])

    def test_a_drive_failure_raises_so_the_outbox_retries_and_nothing_is_marked_done(self):
        self.google.fail("POST", "/upload/drive/v3/files", 500, times=10)
        with self.assertRaises(DriveError):
            asyncio.run(drive_archive.archive("output", self.output.id))
        self.db.expire_all()
        self.assertIsNone(self.db.get(OrderOutput, self.output.id).drive_file_id)

    def test_refused_credentials_fail_without_leaking_the_key(self):
        self.google.refuse_token = True
        with self.assertRaises(Exception) as ctx:
            asyncio.run(drive_archive.archive("output", self.output.id))
        self.assertNotIn("PRIVATE KEY", str(ctx.exception))

    def test_it_does_nothing_unless_switched_on(self):
        with patch.object(settings, "DRIVE_ENABLED", False):
            self.assertFalse(drive_archive.configured())
            self.assertTrue(asyncio.run(drive_archive.archive("output", self.output.id)))
        self.assertEqual(self.uploaded(), [])

    def test_an_inbound_photo_message_is_archived_too(self):
        from app.models.image import Image

        img_path = self.tmp_dir / "req1" / "orig.jpg"
        img_path.parent.mkdir(parents=True)
        img_path.write_bytes(b"JPEG")
        img = Image(request_id="req1", original_filename="o.jpg", stored_filename="orig.jpg", file_path=str(img_path),
                    file_size=4, mime_type="image/jpeg", image_url="/u", processing_status="stored")
        self.db.add(img)
        self.db.commit()
        msg = ChatMessage(customer_phone=SENDER, direction="in", msg_type="image", image_id=img.id, ingestion_id="i1")
        self.db.add(msg)
        self.db.commit()
        self.assertTrue(asyncio.run(drive_archive.archive("photo", msg.id)))
        self.db.expire_all()
        self.assertTrue(self.db.get(ChatMessage, msg.id).drive_file_id)

    def test_the_token_is_reused_between_uploads(self):
        async def twice():
            path = str(self.tmp_dir / "outputs" / "i1" / "a.png")
            await drive_archive.upload_file(path, "a.png", "image/png")
            await drive_archive.upload_file(path, "b.png", "image/png")

        asyncio.run(twice())
        self.assertEqual(self.google.token_requests, 1)


class DeleteTests(_Base):
    def test_copies_are_deleted_and_an_already_gone_copy_counts(self):
        path = str(Path(self.tmp_dir) / "outputs" / "i1" / "a.png")
        file_id = asyncio.run(drive_archive.upload_file(path, "a.png", "image/png"))["id"]
        self.assertEqual(drive_archive.delete_files_sync([file_id, None, "gone"]), 2)
        self.assertNotIn(file_id, [i for i, f in self.google.files.items() if not f["trashed"]])

    def test_nothing_happens_without_the_service_account(self):
        with patch.object(settings, "GOOGLE_SA_KEY_FILE", ""):
            self.assertEqual(drive_archive.delete_files_sync(["a1"]), 0)


if __name__ == "__main__":
    unittest.main()
