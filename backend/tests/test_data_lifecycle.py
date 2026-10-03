"""DATA-7 retention and PRIV-3 erasure."""

import asyncio
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, patch

from app.api.routes import meta_webhook
from app.config import settings
from app.models.audit_log import AuditLog
from app.models.customer import Customer
from app.models.image import Image
from app.models.wallet_transaction import WalletTransaction
from app.models.whatsapp_ingestion import WhatsAppIngestion
from app.services import data_lifecycle as dl
from tests.test_wallet_ledger import SENDER, LedgerTestBase, assert_ledger_matches_balances

OLD = datetime.now(timezone.utc) - timedelta(days=200)


class _Base(LedgerTestBase):
    def make_image(self, request_id):
        image = Image(request_id=request_id, original_filename="a.jpg", stored_filename="a.jpg",
                      file_path=f"/uploads/{request_id}/a.jpg", file_size=10, mime_type="image/jpeg",
                      image_url=f"/uploads/{request_id}/a.jpg", processing_status="stored")
        self.db.add(image)
        self.db.commit()
        return image

    def age(self, row):
        row.created_at = OLD
        self.db.commit()


class MaskTests(unittest.TestCase):
    def test_phone_numbers_are_masked_and_amounts_are_not(self):
        self.assertEqual(dl.mask_numbers("credited 500 to 919812345678 ok"), "credited 500 to 91******5678 ok")
        self.assertEqual(dl.mask_numbers("+919812345678"), "91******5678")
        self.assertEqual(dl.mask_numbers("amount 12345 id 9876"), "amount 12345 id 9876")
        self.assertIsNone(dl.mask_numbers(None))

    def test_masking_twice_changes_nothing(self):
        once = dl.mask_numbers("919812345678")
        self.assertEqual(dl.mask_numbers(once), once)


class RetentionTests(_Base):
    def test_old_photos_of_finished_orders_are_expired_and_recent_or_unfinished_ones_are_kept(self):
        old_done = self.make_ingestion(status="delivered", message_id="m1", image_id=self.make_image("req-1").id)
        old_busy = self.make_ingestion(status="processing", message_id="m2", image_id=self.make_image("req-2").id)
        recent = self.make_ingestion(status="delivered", message_id="m3", image_id=self.make_image("req-3").id)
        self.age(old_done), self.age(old_busy)
        with patch.object(dl, "_delete_image_files") as delete:
            self.assertEqual(dl.purge_expired_media(self.db, days=90), 1)
            self.assertEqual(dl.purge_expired_media(self.db, days=90), 0)          # nothing left to do
        self.assertEqual([c.args[0].request_id for c in delete.call_args_list], ["req-1"])
        self.db.expire_all()
        status = {i.request_id: i.processing_status for i in self.db.query(Image).all()}
        self.assertEqual(status, {"req-1": "expired", "req-2": "stored", "req-3": "stored"})
        self.assertIsNotNone(recent)

    def test_zero_days_turns_it_off(self):
        self.assertEqual(dl.purge_expired_media(self.db, days=0), 0)

    def test_old_audit_details_are_masked_and_recent_ones_untouched(self):
        old = AuditLog(action="x", status="success", details="paid by 919812345678", resource_id="a")
        new = AuditLog(action="x", status="success", details="paid by 919812345678", resource_id="b")
        self.db.add_all([old, new])
        self.db.commit()
        old.created_at = OLD
        self.db.commit()
        self.assertEqual(dl.mask_old_audit_details(self.db, days=30), 1)
        self.db.expire_all()
        details = {r.resource_id: r.details for r in self.db.query(AuditLog).all()}
        self.assertEqual(details["a"], "paid by 91******5678")
        self.assertEqual(details["b"], "paid by 919812345678")

    def test_masking_reaches_rows_beyond_the_first_page_and_spares_pending_reviews(self):
        for i in range(7):
            self.db.add(AuditLog(action="x", status="success", details=f"p 91981234567{i}", resource_id=f"r{i}"))
        self.db.add(AuditLog(action="razorpay_payment_review", status="pending", details="payer 919812345678", resource_id="keep"))
        self.db.commit()
        self.db.query(AuditLog).update({"created_at": OLD})
        self.db.commit()
        self.assertEqual(dl.mask_old_audit_details(self.db, days=30, batch=3), 7)
        self.db.expire_all()
        keep = self.db.query(AuditLog).filter(AuditLog.resource_id == "keep").one()
        self.assertEqual(keep.details, "payer 919812345678")

    def test_the_pass_does_nothing_unless_enabled(self):
        with patch.object(settings, "RETENTION_ENABLED", False):
            self.assertEqual(dl.run_retention_pass(), {"photos": 0, "audit_rows": 0})

    def test_financial_rows_are_never_touched_by_retention(self):
        customer = self.make_customer(500)
        rows_before = self.db.query(WalletTransaction).count()
        dl.mask_old_audit_details(self.db, days=1)
        dl.purge_expired_media(self.db, days=1)
        self.assertEqual(self.db.query(WalletTransaction).count(), rows_before)
        assert_ledger_matches_balances(self, self.db)
        self.assertIsNotNone(customer)


class ErasureTests(_Base):
    def test_a_customer_with_money_left_is_refused_and_nothing_changes(self):
        customer = self.make_customer(500)
        with self.assertRaises(dl.ErasureRefused) as ctx:
            dl.erase_customer(self.db, customer)
        self.assertIn("₹500", str(ctx.exception))
        self.db.expire_all()
        self.assertEqual(self.db.get(Customer, customer.id).full_name, "T")

    def test_team_accounts_are_refused(self):
        customer = self.make_customer(0)
        customer.tier = "ADMIN"
        self.db.commit()
        with self.assertRaises(dl.ErasureRefused):
            dl.erase_customer(self.db, customer)

    def test_erasure_removes_personal_data_and_keeps_the_money_trail(self):
        customer = self.make_customer(0)
        ingestion = self.make_ingestion(status="delivered", image_id=self.make_image("req-9").id, caption="my ring")
        self.db.add(AuditLog(action="note", status="success", details=f"customer {SENDER} did a thing", resource_id="z"))
        self.db.commit()
        ledger_rows = self.db.query(WalletTransaction).count()
        with patch.object(dl, "_delete_image_files") as delete:
            result = dl.erase_customer(self.db, customer)
        delete.assert_called_once()
        self.assertEqual((result["photos"], result["orders"], result["audit_rows"]), (1, 1, 1))
        self.db.expire_all()
        row = self.db.get(Customer, customer.id)
        self.assertEqual((row.full_name, row.business_name, row.gst_number, row.address),
                         ("Deleted customer", "-", "-", "-"))
        self.assertTrue(row.whatsapp_id.startswith("erased-"))
        self.assertNotIn(SENDER, row.whatsapp_id)
        order = self.db.get(WhatsAppIngestion, ingestion.id)
        self.assertEqual((order.caption, order.external_media_id), (None, None))
        self.assertTrue(order.external_user_id.startswith("erased-"))
        self.assertEqual(self.db.get(Image, order.image_id).processing_status, "expired")
        self.assertNotIn(SENDER, self.db.query(AuditLog).filter(AuditLog.resource_id == "z").one().details)
        self.assertEqual(self.db.query(WalletTransaction).count(), ledger_rows)       # the money trail is intact
        self.assertEqual(self.db.query(AuditLog).filter(AuditLog.action == dl.ERASED_ACTION).count(), 1)
        assert_ledger_matches_balances(self, self.db)

    def test_erasure_waits_while_an_order_is_in_progress(self):
        customer = self.make_customer(0)
        self.make_ingestion(status="processing", message_id="wamid.busy")
        with self.assertRaises(dl.ErasureRefused) as ctx:
            dl.erase_customer(self.db, customer)
        self.assertIn("in progress", str(ctx.exception))
        self.db.expire_all()
        self.assertEqual(self.db.get(Customer, customer.id).full_name, "T")

    def test_a_longer_number_containing_the_phone_is_not_rewritten(self):
        customer = self.make_customer(0)
        self.db.add(AuditLog(action="note", status="success", details="ref 99" + SENDER + "7 ok", resource_id="long"))
        self.db.commit()
        dl.erase_customer(self.db, customer)
        self.db.expire_all()
        self.assertEqual(self.db.query(AuditLog).filter(AuditLog.resource_id == "long").one().details, "ref 99" + SENDER + "7 ok")

    def test_the_commands_are_recognised_loosely_but_exactly(self):
        for text in ("DELETE MY DATA", "  delete   my data. ", "Delete my data!"):
            self.assertTrue(dl.is_erasure_request(text), text)
        for text in ("please delete my data now", "delete", "delete my data and more"):
            self.assertFalse(dl.is_erasure_request(text), text)
        self.assertTrue(dl.is_erasure_confirmation("confirm delete"))
        self.assertFalse(dl.is_erasure_confirmation("delete my data"))


class CommandFlowTests(_Base):
    def setUp(self):
        super().setUp()
        self.sent = AsyncMock()
        p = patch.object(meta_webhook, "send_whatsapp_text", new=self.sent)
        p.start()
        self.addCleanup(p.stop)

    def run_command(self, text):
        # the erasure itself runs in its own session; point it at this test's database
        with patch("app.database.SessionLocal", return_value=self.db), patch.object(self.db, "close"):
            asyncio.run(meta_webhook._handle_erasure_command(self.db, SENDER, text))
        return self.sent.await_args.args[1]

    def test_the_confirmation_alone_does_nothing(self):
        customer = self.make_customer(0)
        self.assertEqual(self.run_command("CONFIRM DELETE"), dl.ERASURE_EXPIRED_MESSAGE)
        self.db.expire_all()
        self.assertEqual(self.db.get(Customer, customer.id).full_name, "T")

    def test_ask_then_confirm_erases(self):
        customer = self.make_customer(0)
        self.assertEqual(self.run_command("DELETE MY DATA"), dl.ERASURE_ASK_MESSAGE)
        self.assertEqual(self.run_command("CONFIRM DELETE"), dl.ERASURE_DONE_MESSAGE)
        self.db.expire_all()
        self.assertEqual(self.db.get(Customer, customer.id).full_name, "Deleted customer")

    def test_a_request_older_than_the_window_is_not_honoured(self):
        customer = self.make_customer(0)
        dl.request_erasure(self.db, customer)
        self.db.query(AuditLog).update({"created_at": datetime.now(timezone.utc) - timedelta(minutes=30)})
        self.db.commit()
        self.assertEqual(self.run_command("CONFIRM DELETE"), dl.ERASURE_EXPIRED_MESSAGE)

    def test_a_stranger_is_told_there_is_nothing_to_delete(self):
        self.assertEqual(self.run_command("DELETE MY DATA"), dl.ERASURE_NOTHING_MESSAGE)

    def test_a_customer_with_money_is_told_to_contact_support(self):
        self.make_customer(500)
        self.run_command("DELETE MY DATA")
        self.assertIn("not refunded", self.run_command("CONFIRM DELETE"))


if __name__ == "__main__":
    unittest.main()
