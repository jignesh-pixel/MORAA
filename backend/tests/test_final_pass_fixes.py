"""Focused tests for the final fix pass (payments, lookups, config, feedback, alerts)."""

import json
import unittest
from unittest.mock import patch

from app.api.routes import meta_webhook
from app.config import settings
from app.models.audit_log import AuditLog
from app.models.customer import Customer
from app.models.whatsapp_ingestion import WhatsAppIngestion
from app.services import generation_metrics, meta_whatsapp_service, wallet_service
from tests.test_razorpay_webhook import (
    SENDER,
    RazorpayWebhookRouteTests,
    _payment_captured_payload,
)
from tests.test_wallet_funded_slot_gate import _make_customer, _make_engine_and_session


class PaymentIdempotencyTests(unittest.TestCase):
    setUp = RazorpayWebhookRouteTests.setUp
    tearDown = RazorpayWebhookRouteTests.tearDown

    def _pay(self, payment_id="pay_first0000001", amount=50000, contact=SENDER):
        return self.client.post(
            "/api/payments/razorpay/webhook",
            json=_payment_captured_payload(payment_id=payment_id, amount=amount, contact=contact),
        ).json()

    def _balance(self):
        return wallet_service.get_balance(self.session, SENDER)

    def test_first_payment_credits(self):
        _make_customer(self.session, balance=0)
        self.assertEqual(self._pay()["status"], "ok")
        self.assertEqual(self._balance(), 500)

    def test_duplicate_payment_does_not_credit_again(self):
        _make_customer(self.session, balance=0)
        self._pay()
        self.assertEqual(self._pay()["status"], "already_processed")
        self.assertEqual(self._balance(), 500)

    def test_concurrent_duplicate_rejected_by_database(self):
        """Both requests pass the pre-check (the race); the unique index stops the 2nd."""
        _make_customer(self.session, balance=0)
        self._pay()
        with patch.object(self.webhook_module, "_already_processed", side_effect=[False, True]):
            self.assertEqual(self._pay()["status"], "already_processed")
        self.assertEqual(self._balance(), 500)
        rows = self.session.query(AuditLog).filter(AuditLog.action == "razorpay_payment_captured").count()
        self.assertEqual(rows, 1)

    def test_separate_payments_both_credit(self):
        _make_customer(self.session, balance=0)
        self._pay("pay_first0000001")
        self._pay("pay_second000002")
        self.assertEqual(self._balance(), 1000)

    def test_unmatched_payer_is_held_unregistered_not_fake_registered(self):
        self.assertEqual(self._pay(contact="918888877777")["status"], "ok")
        c = self.session.query(Customer).filter(Customer.whatsapp_id == "918888877777").one()
        self.assertFalse(c.is_registered)
        self.assertEqual(c.wallet_balance, 500)
        audit = self.session.query(AuditLog).filter(AuditLog.action == "razorpay_payment_captured").one()
        self.assertTrue(json.loads(audit.details)["auto_provisioned"])


class PhoneLookupTests(unittest.TestCase):
    def setUp(self):
        self.engine, self.db = _make_engine_and_session()
        _make_customer(self.db, balance=0)  # stored as 919876543210

    def tearDown(self):
        self.db.close()
        self.engine.dispose()

    def test_variants_match_exactly(self):
        for phone in ("919876543210", "+919876543210", "9876543210"):
            self.assertEqual(wallet_service.find_customer_by_phone(self.db, phone).whatsapp_id, SENDER)

    def test_unrelated_number_does_not_match(self):
        self.assertIsNone(wallet_service.find_customer_by_phone(self.db, "919876543211"))
        self.assertIsNone(wallet_service.find_customer_by_phone(self.db, ""))


class FeedbackPersistenceTests(unittest.TestCase):
    def setUp(self):
        self.engine, self.db = _make_engine_and_session()
        self.ing = WhatsAppIngestion(
            external_user_id=SENDER, external_message_id="wamid.fb", external_media_id="m",
            channel="whatsapp", status="delivered",
        )
        self.db.add(self.ing)
        self.db.commit()

    def tearDown(self):
        self.db.close()
        self.engine.dispose()

    def test_feedback_saved_against_latest_ingestion(self):
        meta_webhook._save_feedback(self.db, SENDER, "feedback_negative")
        row = self.db.query(AuditLog).filter(AuditLog.action == "whatsapp_feedback").one()
        self.assertEqual((row.status, row.resource_id), ("negative", self.ing.id))

    def test_feedback_with_ingestion_id_suffix(self):
        meta_webhook._save_feedback(self.db, SENDER, f"feedback_positive_{self.ing.id}")
        row = self.db.query(AuditLog).filter(AuditLog.action == "whatsapp_feedback").one()
        self.assertEqual((row.status, row.resource_id), ("positive", self.ing.id))


class ConfigAndAlertTests(unittest.TestCase):
    def test_style_limit_comes_from_settings_and_stays_1(self):
        self.assertEqual(settings.MAX_STYLES_PER_PACK, 1)
        self.assertEqual(meta_whatsapp_service.MAX_STYLES_PER_PACK, settings.MAX_STYLES_PER_PACK)

    def test_old_send_names_still_work(self):
        m = meta_whatsapp_service
        self.assertIs(m.send_6_pack_images_to_whatsapp, m.send_catalog_pack_images_to_whatsapp)
        self.assertIs(m.send_7_pack_images_to_whatsapp, m.send_catalog_pack_images_to_whatsapp)

    def test_failure_rate_alert_logged_when_above_target(self):
        engine, db = _make_engine_and_session()
        for i, status in enumerate(["failed", "failed", "delivered"]):
            db.add(WhatsAppIngestion(external_user_id=SENDER, external_message_id=f"w{i}",
                                     external_media_id="m", channel="whatsapp", status=status))
        db.commit()
        with patch.object(generation_metrics.logger, "warning") as warn:
            report = generation_metrics.alert_if_failure_rate_exceeded(db)
        self.assertTrue(report.exceeds_target)
        self.assertIn("GENERATION_FAILURE_RATE_ALERT", warn.call_args[0][0])
        db.close()
        engine.dispose()


if __name__ == "__main__":
    unittest.main()
