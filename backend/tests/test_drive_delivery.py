"""Phase 8 delivery: SKU credit intake, Drive upload of finished images, WhatsApp fallback, credit refund once, and the
one debounced "ready" message. Drive and Sheets are the in-memory fake from tests/test_google_drive.py."""

import asyncio
import re
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, patch

from app.config import settings
from app.models.audit_log import AuditLog
from app.models.customer import Customer
from app.models.outbox_job import OutboxJob
from app.models.sku_credit import ACTION_CONSUME, ACTION_REFUND, CustomerSkuCredit
from app.models.whatsapp_ingestion import WhatsAppIngestion
from app.services import batch_notify, drive_delivery, outbox, sku_packs
from app.services import meta_whatsapp_service as mws
from tests.test_google_drive import PHONE, DriveDbTestBase
from tests.test_wallet_funded_slot_gate import SENDER, FundedSlotGateTestCase, _image_payload, _make_customer


class DeliveryBase(DriveDbTestBase):
    def setUp(self):
        super().setUp()
        p = patch.object(settings, "BATCH_NOTIFY_DELAY_SECONDS", 60)
        p.start()
        self.addCleanup(p.stop)
        self.customer_id = self.make_customer(email="buyer@gmail.com")
        with self.Session() as db:
            sku_packs.grant_pack_credits(db, db.get(Customer, self.customer_id), 5, "pay_test_5")
            db.commit()

    def order(self, status="generated", consume=True, message_id="wamid.p1"):
        """A white-background order paid by one credit, in ``status``. Returns its id."""
        with self.Session() as db:
            row = WhatsAppIngestion(external_user_id=PHONE, external_message_id=message_id, status=status,
                                    product_code="WHITE_BG", credit_source="sku")
            db.add(row)
            db.commit()
            if consume:
                assert sku_packs.consume_credit(db, db.get(Customer, self.customer_id), row.id)
                db.commit()
            return row.id

    def status(self, ingestion_id):
        with self.Session() as db:
            row = db.get(WhatsAppIngestion, ingestion_id)
            return row.status, row.delivery_channel, row.drive_file_id

    def credits(self):
        with self.Session() as db:
            return sku_packs.balance(db, self.customer_id)

    def jobs(self, kind):
        with self.Session() as db:
            return db.query(OutboxJob).filter(OutboxJob.kind == kind).all()


class DriveUploadTests(DeliveryBase):
    def test_hand_over_moves_the_order_and_records_one_job(self):
        order = self.order()
        job_id = drive_delivery.hand_over(order, self.image(), "image/png")
        self.assertIsNotNone(job_id)
        self.assertEqual(self.status(order)[:2], ("drive_pending", "drive"))
        self.assertIsNone(drive_delivery.hand_over(order, self.image(), "image/png"))     # not "generated" any more
        self.assertEqual(len(self.jobs("drive_deliver")), 1)

    def test_upload_goes_to_the_day_folder_numbered_and_schedules_one_ready_check(self):
        first, second = self.order(message_id="wamid.a"), self.order(message_id="wamid.b")
        with patch.object(batch_notify, "schedule") as schedule:
            for order in (first, second):
                job_id = drive_delivery.hand_over(order, self.image(f"{order}.png"), "image/png")
                self.assertTrue(asyncio.run(outbox.run_job_now(job_id)))
        names = sorted(f["name"] for f in self.google.files.values() if f["name"].endswith(".png"))
        self.assertEqual(names, ["001.png", "002.png"])
        day = [f for f in self.google.files.values() if f["mimeType"].endswith("folder") and re.fullmatch(r"\d{1,2} [A-Z][a-z]{2} \d{4}", f["name"])]
        self.assertEqual(len(day), 1)                                  # one folder for today (IST)
        for order in (first, second):
            status, channel, file_id = self.status(order)
            self.assertEqual((status, channel), ("delivered", "drive"))
            self.assertTrue(file_id)
        self.assertEqual(schedule.call_count, 2)
        self.assertEqual(self.credits(), 3)                            # 5 bought, 2 used, none returned

    def test_a_retryable_drive_failure_raises_so_the_outbox_retries(self):
        order = self.order()
        job_id = drive_delivery.hand_over(order, self.image(), "image/png")
        self.google.fail("POST", "/upload/drive/v3/files", 503, times=10)
        asyncio.run(outbox.run_job_now(job_id))
        [job] = self.jobs("drive_deliver")
        self.assertEqual(job.status, "pending")                         # back in the queue with a back-off
        self.assertEqual(self.status(order)[0], "drive_pending")


class FallbackTests(DeliveryBase):
    def test_a_dead_drive_job_sends_the_image_on_whatsapp(self):
        order = self.order()
        drive_delivery.hand_over(order, self.image(), "image/png")
        with patch.object(mws, "upload_media_to_meta", new=AsyncMock(return_value="media1")), \
                patch.object(mws, "send_image_to_whatsapp", new=AsyncMock(return_value=True)) as send:
            self.assertTrue(asyncio.run(drive_delivery.handle_dead_job({"ingestion_id": order, "path": self.image()})))
        send.assert_awaited_once()
        self.assertEqual(self.status(order)[:2], ("delivered", "whatsapp"))
        self.assertEqual(self.credits(), 4)                            # the credit stays used: the image arrived

    def test_drive_and_whatsapp_both_failing_returns_the_credit_exactly_once(self):
        order = self.order()
        drive_delivery.hand_over(order, self.image(), "image/png")
        with patch.object(mws, "upload_media_to_meta", new=AsyncMock(return_value=None)), \
                patch.object(mws, "send_whatsapp_text", new=AsyncMock(return_value=True)) as notice:
            asyncio.run(drive_delivery.handle_dead_job({"ingestion_id": order, "path": self.image()}))
            asyncio.run(drive_delivery.handle_dead_job({"ingestion_id": order, "path": self.image()}))
        self.assertEqual(self.status(order)[0], "delivery_failed")
        self.assertEqual(self.credits(), 5)
        with self.Session() as db:
            refunds = db.query(CustomerSkuCredit).filter(CustomerSkuCredit.action == ACTION_REFUND).count()
        self.assertEqual(refunds, 1)
        self.assertIn("SKU credit has been returned", notice.await_args.args[1])

    def test_drive_switched_off_while_waiting_delivers_on_whatsapp(self):
        order = self.order()
        job_id = drive_delivery.hand_over(order, self.image(), "image/png")
        with patch.object(settings, "DRIVE_DELIVERY_ENABLED", False), \
                patch.object(mws, "upload_media_to_meta", new=AsyncMock(return_value="media1")), \
                patch.object(mws, "send_image_to_whatsapp", new=AsyncMock(return_value=True)):
            asyncio.run(outbox.run_job_now(job_id))
        self.assertEqual(self.status(order)[:2], ("delivered", "whatsapp"))

    def test_a_stalled_drive_order_without_a_job_is_delivered_by_the_sweep(self):
        order = self.order()
        drive_delivery.hand_over(order, self.image(), "image/png")
        with self.Session() as db:
            db.query(OutboxJob).update({OutboxJob.status: "dead"})          # the job gave up and its fallback was lost
            db.query(WhatsAppIngestion).filter(WhatsAppIngestion.id == order).update(
                {WhatsAppIngestion.updated_at: datetime.now(timezone.utc) - timedelta(hours=4)})
            db.commit()
        with patch.object(mws, "upload_media_to_meta", new=AsyncMock(return_value="media1")), \
                patch.object(mws, "send_image_to_whatsapp", new=AsyncMock(return_value=True)):
            self.assertEqual(asyncio.run(drive_delivery.recover_stalled_deliveries()), 1)
        self.assertEqual(self.status(order)[0], "delivered")


class BatchNotifyTests(DeliveryBase):
    def deliver(self, order):
        job_id = drive_delivery.hand_over(order, self.image(f"{order}.png"), "image/png")
        with patch.object(batch_notify, "schedule"):
            asyncio.run(outbox.run_job_now(job_id))

    def test_one_message_with_the_folder_link_and_skus_left_when_nothing_is_in_progress(self):
        a, b = self.order(message_id="wamid.a"), self.order(message_id="wamid.b")
        self.deliver(a)
        self.deliver(b)
        with patch.object(mws, "send_whatsapp_text", new=AsyncMock(return_value=True)) as send:
            asyncio.run(batch_notify.handle_outbox_job({"customer_id": self.customer_id}))
            asyncio.run(batch_notify.handle_outbox_job({"customer_id": self.customer_id}))   # nothing new
        send.assert_awaited_once()
        text = send.await_args.args[1]
        self.assertIn("https://drive.google.com/drive/folders/", text)
        self.assertIn("3 SKUs left", text)

    def test_waits_while_an_order_is_still_in_progress(self):
        self.deliver(self.order(message_id="wamid.a"))
        self.order(status="processing", message_id="wamid.b")
        with patch.object(mws, "send_whatsapp_text", new=AsyncMock(return_value=True)) as send, \
                patch.object(batch_notify, "schedule") as again:
            asyncio.run(batch_notify.handle_outbox_job({"customer_id": self.customer_id}))
        send.assert_not_awaited()
        again.assert_called_once_with(self.customer_id, 1)

    def test_outside_the_24_hour_window_the_template_is_used(self):
        order = self.order()
        with self.Session() as db:
            db.query(WhatsAppIngestion).update({WhatsAppIngestion.created_at: datetime.now(timezone.utc) - timedelta(days=2)})
            db.commit()
        self.deliver(order)
        with patch.object(settings, "BATCH_NOTIFY_TEMPLATE_NAME", "sku_batch_ready"), \
                patch.object(mws, "send_whatsapp_template", new=AsyncMock(return_value=True), create=True) as template, \
                patch.object(mws, "send_whatsapp_text", new=AsyncMock(return_value=True)) as text:
            asyncio.run(batch_notify.handle_outbox_job({"customer_id": self.customer_id}))
        text.assert_not_awaited()
        self.assertEqual(template.await_args.args[1], "sku_batch_ready")
        with self.Session() as db:
            self.assertEqual(db.query(AuditLog).filter(AuditLog.action == batch_notify.NOTIFIED_ACTION).count(), 1)


class CreditLimitTests(DeliveryBase):
    def test_100_orders_against_20_credits_use_exactly_20(self):
        with self.Session() as db:
            customer = db.get(Customer, self.customer_id)
            sku_packs.grant_pack_credits(db, customer, 15, "pay_test_15")
            db.commit()
            used = 0
            for i in range(100):
                if sku_packs.consume_credit(db, customer, f"order-{i}"):
                    used += 1
                db.commit()
            consumed = db.query(CustomerSkuCredit).filter(CustomerSkuCredit.action == ACTION_CONSUME).count()
        self.assertEqual((used, consumed, self.credits()), (20, 20, 0))


class SkuIntakeTests(FundedSlotGateTestCase):
    """A photo from a customer with SKU credits: no buttons, one credit, one queued Clean Studio Shot."""

    def setUp(self):
        super().setUp()
        self.runs = AsyncMock(return_value=True)
        for p in (patch.object(self.webhook_module, "process_whatsapp_white_bg", new=self.runs),
                  patch.object(self.webhook_module, "generation_capacity_blocked", return_value=None),
                  patch.object(settings, "OUTBOX_ENABLED", False)):
            p.start()
            self.addCleanup(p.stop)

    def grant(self, units):
        customer = self.session.query(Customer).filter(Customer.whatsapp_id == SENDER).one()
        sku_packs.grant_pack_credits(self.session, customer, units, f"pay_{units}")
        self.session.commit()
        return customer

    def test_each_photo_uses_one_credit_and_queues_a_studio_shot(self):
        customer = _make_customer(self.session, balance=0)
        self.grant(2)
        self.client.post("/api/meta/webhook", json=_image_payload(count=3))
        rows = self.session.query(WhatsAppIngestion).order_by(WhatsAppIngestion.external_message_id).all()
        self.assertEqual([r.status for r in rows][:2], ["white_queued", "white_queued"])
        self.assertTrue(all(r.credit_source == "sku" and r.amount_charged is None for r in rows[:2]))
        self.assertEqual(sku_packs.balance(self.session, customer.id), 0)
        self.assertEqual(self.runs.await_count, 2)
        self.assertEqual(self.webhook_module.send_product_selection_buttons.await_count, 0)
        self.assertEqual(customer.wallet_balance, 0)
        self.assertTrue(any("1 SKU used" in t for t in self.sent_texts))

    def test_without_credits_the_wallet_flow_is_unchanged(self):
        _make_customer(self.session, balance=1000)
        self.client.post("/api/meta/webhook", json=_image_payload(count=1))
        self.assertEqual(self.session.query(WhatsAppIngestion).one().status, "awaiting_choice")
        self.runs.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()


class EmailReminderTests(SkuIntakeTests):
    def test_a_photo_from_a_customer_without_email_is_processed_and_reminds_once_in_the_receipt(self):
        _make_customer(self.session, balance=0)
        self.grant(1)
        self.client.post("/api/meta/webhook", json=_image_payload(count=1))
        self.assertEqual(self.session.query(WhatsAppIngestion).one().status, "white_queued")
        receipt = [t for t in self.sent_texts if "1 SKU used" in t][0]
        self.assertIn("email address", receipt)
