"""Focused tests: refund on failed generation + prevalidation before charge."""

import asyncio
import os
import tempfile
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from app.config import settings
from app.models.audit_log import AuditLog
from app.models.image import Image
from app.models.whatsapp_ingestion import WhatsAppIngestion
from app.services import meta_whatsapp_service as mws
from app.services import wallet_service
from tests.test_wallet_funded_slot_gate import (
    SENDER,
    FundedSlotGateTestCase,
    _image_payload,
    _make_customer,
    _make_engine_and_session,
)

PRICE = wallet_service.price_per_image()


class RefundOnFailedGenerationTests(unittest.TestCase):
    def setUp(self):
        self.engine, self.db = _make_engine_and_session()
        # Customer already charged one slot: balance after charge = 200.
        _make_customer(self.db, balance=200)
        fd, self.path = tempfile.mkstemp(suffix=".jpg")
        os.write(fd, b"\xff\xd8\xff" + b"\x00" * 64)
        os.close(fd)
        img = Image(
            original_filename="x.jpg", stored_filename="x.jpg", file_path=self.path,
            file_size=67, mime_type="image/jpeg", image_url="/x.jpg",
        )
        self.db.add(img)
        self.db.commit()
        self.ingestion = WhatsAppIngestion(
            external_user_id=SENDER, external_message_id="wamid.r1", external_media_id="m1",
            channel="whatsapp", image_id=img.id, status="pack_queued",
        )
        self.db.add(self.ingestion)
        self.db.commit()
        self.patches = [
            patch("app.database.SessionLocal", return_value=self.db),
            patch.object(self.db, "close"),
            patch.object(mws, "DRY_RUN_IMAGE_MODE", False),
            patch.object(mws, "send_whatsapp_text", new=AsyncMock(return_value=True)),
        ]
        for p in self.patches:
            p.start()

    def tearDown(self):
        for p in reversed(self.patches):
            p.stop()
        self.db.close()
        self.engine.dispose()
        os.remove(self.path)

    def _balance(self):
        return wallet_service.get_balance(self.db, SENDER)

    def _refund_rows(self):
        return self.db.query(AuditLog).filter(AuditLog.action == mws.REFUND_AUDIT_ACTION).count()

    def _run(self, generated):
        with patch.object(mws, "_generate_single_pack_style", new=AsyncMock(return_value=generated)), \
             patch.object(mws, "upload_media_to_meta", new=AsyncMock(return_value="media-1")), \
             patch.object(mws, "send_image_to_whatsapp", new=AsyncMock(return_value=True)):
            return asyncio.run(mws.process_whatsapp_catalog_pack(self.ingestion.id))

    def test_generation_success_no_refund(self):
        self.assertTrue(self._run("data:image/png;base64,AAAA"))
        self.assertEqual(self.ingestion.status, "delivered")
        self.assertEqual(self._balance(), 200)
        self.assertEqual(self._refund_rows(), 0)

    def test_generation_failure_refunds_once(self):
        self.assertFalse(self._run(None))
        self.assertEqual(self.ingestion.status, "failed")
        self.assertEqual(self._balance(), 200 + PRICE)
        self.assertEqual(self._refund_rows(), 1)

    def test_duplicate_failure_no_double_refund(self):
        self._run(None)
        self.ingestion.status = "failed"  # manual retry of a failed ingestion
        self.db.commit()
        self._run(None)
        mws._fail_ingestion(self.db, self.ingestion, "again")
        self.assertEqual(self._balance(), 200 + PRICE)
        self.assertEqual(self._refund_rows(), 1)


class PrevalidationBeforeChargeTests(FundedSlotGateTestCase):
    def _post(self, enabled, approved):
        self.webhook_module.process_whatsapp_catalog_pack.reset_mock()
        check = AsyncMock(return_value=MagicMock(approved=approved, rejection_message="bad photo"))
        with patch.object(settings, "IMAGE_PREVALIDATION_ENABLED", enabled), \
             patch.object(self.webhook_module, "check_image_quality", new=check):
            self.assertEqual(self.client.post("/api/meta/webhook", json=_image_payload()).status_code, 200)
        self._choose("gv_pack1")  # the customer picks E-Com Pack 1
        return check

    def test_rejection_no_charge_no_generation(self):
        _make_customer(self.session, balance=700)
        check = self._post(enabled=True, approved=False)
        check.assert_awaited_once()
        self.assertEqual(self._balance(), 700)
        self.assertEqual(self._statuses(), ["rejected"])
        self.webhook_module.process_whatsapp_catalog_pack.assert_not_called()
        self.assertIn("bad photo", self.sent_texts)

    def test_pass_charges_and_generation_continues(self):
        _make_customer(self.session, balance=700)
        check = self._post(enabled=True, approved=True)
        check.assert_awaited_once()
        self.assertEqual(self._balance(), 700 - PRICE)
        self.assertEqual(self._statuses(), ["pack_queued"])
        self.webhook_module.process_whatsapp_catalog_pack.assert_called_once()

    def test_disabled_skips_check_and_generation_continues(self):
        _make_customer(self.session, balance=700)
        check = self._post(enabled=False, approved=False)
        check.assert_not_awaited()
        self.assertEqual(self._balance(), 700 - PRICE)
        self.assertEqual(self._statuses(), ["pack_queued"])


if __name__ == "__main__":
    unittest.main()
