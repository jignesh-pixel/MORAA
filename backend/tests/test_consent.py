"""PRIV-2: consent to the data notice before personal data is collected."""

import asyncio
import unittest
from unittest.mock import AsyncMock, patch

from app.api.routes import meta_webhook
from app.config import settings
from app.models.consent_record import ConsentRecord
from app.models.whatsapp_ingestion import WhatsAppIngestion
from app.services import consent_service
from tests.test_wallet_ledger import SENDER, LedgerTestBase

NOTICE = "We keep your photos and details to make your images. Tap I agree to continue."


class _Base(LedgerTestBase):
    def setUp(self):
        super().setUp()
        self.sent = AsyncMock()
        self.buttons = AsyncMock(return_value=True)
        for p in (patch.object(meta_webhook, "send_whatsapp_text", new=self.sent),
                  patch("app.services.meta_whatsapp_service.send_reply_buttons", new=self.buttons)):
            p.start()
            self.addCleanup(p.stop)

    def on(self):
        for p in (patch.object(settings, "CONSENT_REQUIRED", True), patch.object(settings, "CONSENT_NOTICE_TEXT", NOTICE),
                  patch.object(settings, "CONSENT_VERSION", "1")):
            p.start()
            self.addCleanup(p.stop)


class SwitchTests(_Base):
    def test_off_by_default_nothing_is_gated(self):
        self.assertFalse(consent_service.is_active())
        self.assertFalse(asyncio.run(meta_webhook._consent_gate(self.db, SENDER)))
        self.buttons.assert_not_awaited()

    def test_required_but_no_notice_text_stays_off(self):
        with patch.object(settings, "CONSENT_REQUIRED", True), patch.object(settings, "CONSENT_NOTICE_TEXT", "  "):
            self.assertFalse(consent_service.is_active())


class GateTests(_Base):
    def test_a_new_number_is_shown_the_notice_with_two_buttons_and_the_caller_must_stop(self):
        self.on()
        self.assertTrue(asyncio.run(meta_webhook._consent_gate(self.db, SENDER, "wamid.1")))
        args = self.buttons.await_args
        self.assertEqual(args.args[0], SENDER)
        self.assertEqual(args.args[1], NOTICE)
        self.assertEqual([b[0] for b in args.args[2]], [consent_service.CONSENT_YES, consent_service.CONSENT_NO])

    def test_agreeing_is_stored_once_with_the_version_and_lifts_the_gate(self):
        self.on()
        consent_service.record_consent(self.db, SENDER)
        consent_service.record_consent(self.db, SENDER)                      # a double tap
        rows = self.db.query(ConsentRecord).all()
        self.assertEqual((len(rows), rows[0].version), (1, "1"))
        self.assertFalse(asyncio.run(meta_webhook._consent_gate(self.db, SENDER)))

    def test_a_new_notice_version_asks_again(self):
        self.on()
        consent_service.record_consent(self.db, SENDER)
        with patch.object(settings, "CONSENT_VERSION", "2"):
            self.assertTrue(asyncio.run(meta_webhook._consent_gate(self.db, SENDER)))

    def test_phone_formats_do_not_matter(self):
        self.on()
        consent_service.record_consent(self.db, "+91 98123 45678")
        self.assertTrue(consent_service.has_consented(self.db, "919812345678"))


class PhotoTests(_Base):
    def test_a_photo_from_a_number_that_has_not_agreed_is_not_stored_at_all(self):
        self.on()
        event = {"sender": SENDER, "message_id": "wamid.photo", "media_id": "123", "caption": "", "timestamp": "1"}
        self.assertIsNone(asyncio.run(meta_webhook._ingest_image_for_choice(self.db, event)))
        self.assertEqual(self.db.query(WhatsAppIngestion).count(), 0)
        self.buttons.assert_awaited_once()


if __name__ == "__main__":
    unittest.main()
