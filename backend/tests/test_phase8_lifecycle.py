"""Phase 8: pack invoices (lines, Drive copy, link text), Drive image retention, erasure with credits and a Drive
folder, the dashboard's pack numbers and the Drive alerts."""

from __future__ import annotations

import asyncio

from app.models.customer import Customer
import json
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock, patch

from app.config import settings
from app.models.audit_log import AuditLog
from app.models.chat_log import InvoiceRecord
from app.models.outbox_job import DEAD, OutboxJob
from app.models.sku_credit import ACTION_PURCHASE, SKU_CREATIVE, SKU_WHITE_BG, CustomerSkuCredit
from app.models.whatsapp_ingestion import WhatsAppIngestion
from app.services import alert_service, billing_service, chat_log, dashboard_service, drive_layout, pricing
from app.services import data_lifecycle as dl
from app.services.sku_packs import purchase_reference
from tests.test_billing_erpnext import ERP_SETTINGS, FakeERPNext, _ERPTestBase, _service
from tests.test_google_drive import PHONE, DriveDbTestBase

PDF = b"%PDF-1.7 pack invoice"
PAYMENT = "pay_PACK1"
SEND_TEXT = "app.services.meta_whatsapp_service.send_whatsapp_text"


class Phase8Base(DriveDbTestBase):
    def setUp(self) -> None:
        super().setUp()
        for p in (patch.multiple(settings, **ERP_SETTINGS, SKU_ERPNEXT_ITEM_CODE="MORAA-SKU"),
                  patch.object(chat_log, "fire", side_effect=lambda fn, *args: fn(*args))):
            p.start()
            self.addCleanup(p.stop)
        self.customer_id = self.make_customer(email="buyer@gmail.com")

    def grant(self, units: int, sku: str = SKU_WHITE_BG, payment_id: str = PAYMENT, customer_id: str = None) -> None:
        with self.Session() as db:
            db.add(CustomerSkuCredit(customer_id=customer_id or self.customer_id, sku=sku, action=ACTION_PURCHASE,
                                     quantity=units, balance_after=units,
                                     reference_id=purchase_reference(payment_id, sku)))
            db.commit()

    def order(self, mid: str, status: str = "delivered", drive_file_id: str = None, age: timedelta = None,
              channel: str = None) -> str:
        with self.Session() as db:
            row = WhatsAppIngestion(external_user_id=PHONE, external_message_id=mid, external_media_id="x",
                                    channel="whatsapp", status=status, drive_file_id=drive_file_id,
                                    delivery_channel=channel)
            db.add(row)
            db.commit()
            if age:
                when = datetime.now(timezone.utc) - age
                db.query(WhatsAppIngestion).filter(WhatsAppIngestion.id == row.id).update(
                    {"created_at": when, "updated_at": when})
                db.commit()
            return row.id

    def dispatch(self, amount: int, erp_enabled: bool = True, erp_result=(PDF, "ACC-SINV-0001"), send_ok=True):
        svc = MagicMock()
        svc.create_paid_invoice_pdf = AsyncMock(return_value=erp_result)
        local = MagicMock(return_value=b"%PDF local")
        send_doc = AsyncMock(return_value=send_ok)
        with patch.object(settings, "ERPNEXT_INVOICE_ENABLED", erp_enabled), \
             patch.object(billing_service, "get_erpnext_service", return_value=svc):
            outcome = asyncio.run(billing_service.dispatch_payment_invoice(
                PHONE, PAYMENT, amount, "Anurag", {"full_name": "Anurag"}, local, send_doc))
        return outcome, svc, local, send_doc

    def invoice_record(self) -> InvoiceRecord:
        with self.Session() as db:
            return db.query(InvoiceRecord).filter(InvoiceRecord.payment_id == PAYMENT).one()


# ── pack invoices ──────────────────────────────────────────────────────────────────────────────────────────

class PackInvoiceTests(Phase8Base):
    def setUp(self) -> None:
        super().setUp()
        p = patch.object(settings, "DRIVE_DELIVERY_ENABLED", False)
        p.start()
        self.addCleanup(p.stop)

    @staticmethod
    def total(lines) -> float:
        return sum(line["qty"] * line["rate"] for line in lines)

    def test_a_cart_is_one_line_per_sku_and_the_lines_add_up_to_what_was_paid(self):
        self.grant(20)
        self.grant(2, SKU_CREATIVE)
        creative = pricing.creative_pack_price()
        amount = 20 * 17 + 2 * creative                    # the white price paid differs from today's price
        outcome, svc, local, send_doc = self.dispatch(amount)
        self.assertEqual(outcome, "erpnext")
        lines = svc.create_paid_invoice_pdf.await_args.kwargs["lines"]
        self.assertEqual([(line["item_code"], line["qty"], line["rate"]) for line in lines],
                         [("MORAA-SKU", 20, 17.0), ("MORAA-SKU", 2, float(creative))])
        self.assertEqual(self.total(lines), amount)
        self.assertEqual(send_doc.await_args.kwargs["filename"], "ACC-SINV-0001.pdf")   # Drive off: as before
        local.assert_not_called()

    def test_white_only_and_creative_only_packs(self):
        self.assertEqual(billing_service.pack_invoice_lines(5, 0, 100)[0] | {"description": ""},
                         {"item_code": "MORAA-SKU", "qty": 5, "rate": 20.0, "description": ""})
        creative = billing_service.pack_invoice_lines(0, 3, 3 * pricing.creative_pack_price())
        self.assertEqual((creative[0]["qty"], self.total(creative)), (3, 3 * pricing.creative_pack_price()))
        uneven = billing_service.pack_invoice_lines(3, 0, 100)        # 100 / 3 is not whole rupees
        self.assertEqual(self.total(uneven), 100)

    def test_a_wallet_recharge_is_billed_exactly_as_before(self):
        _outcome, svc, _local, _send = self.dispatch(500)
        self.assertNotIn("lines", svc.create_paid_invoice_pdf.await_args.kwargs)

    def test_the_local_receipt_says_what_was_bought(self):
        self.grant(20)
        outcome, _svc, local, _send = self.dispatch(400, erp_enabled=False)
        self.assertEqual(outcome, "local")
        self.assertIn("20 SKUs", local.call_args.kwargs["description"])
        with self.Session() as db:
            db.query(CustomerSkuCredit).delete()
            db.commit()
        _outcome, _svc, local, _send = self.dispatch(500, erp_enabled=False)
        self.assertNotIn("description", local.call_args.kwargs)


class ERPNextLinesTests(_ERPTestBase):
    def test_the_sales_invoice_carries_the_given_lines(self):
        fake = FakeERPNext()
        lines = [{"item_code": "MORAA-SKU", "qty": 20, "rate": 20.0}, {"item_code": "MORAA-SKU", "qty": 1, "rate": 100.0}]
        result = self._run(fake, lambda: _service().create_paid_invoice_pdf(
            PHONE, "Moraa Jewels", None, 500, PAYMENT, lines=lines))
        self.assertEqual(result, (b"%PDF-1.7 erpnext invoice", "ACC-SINV-0001"))
        self.assertEqual(fake.posts("/api/resource/Sales Invoice")[0][3]["items"], lines)

    def test_a_line_without_an_item_code_is_not_billed(self):
        fake = FakeERPNext()
        result = self._run(fake, lambda: _service().create_paid_invoice_pdf(
            PHONE, "M", None, 500, PAYMENT, lines=[{"item_code": "", "qty": 1, "rate": 500.0}]))
        self.assertIsNone(result)
        self.assertEqual(fake.calls, [])


class InvoiceToDriveTests(Phase8Base):
    def setUp(self):
        super().setUp()
        # Invoices only go to Drive once the customer can open their folder (it is shared with their email).
        asyncio.run(drive_layout.ensure_customer_folders(self.customer_id))
        with self.Session() as db:
            customer = db.get(Customer, self.customer_id)
            customer.email = customer.drive_shared_to = "buyer@gmail.com"
            db.commit()

    def test_an_unshared_folder_gets_the_document_on_whatsapp(self):
        with self.Session() as db:
            db.get(Customer, self.customer_id).drive_shared_to = None
            db.commit()
        with patch(SEND_TEXT, AsyncMock(return_value=True)) as send_text:
            outcome, _svc, _local, send_doc = self.dispatch(500)
        send_doc.assert_awaited_once()
        send_text.assert_not_awaited()

    def test_the_invoice_goes_into_invoices_with_the_dated_name_and_a_link_text(self):
        send_text = AsyncMock(return_value=True)
        with patch(SEND_TEXT, send_text):
            outcome, _svc, _local, send_doc = self.dispatch(500)
        self.assertEqual(outcome, "erpnext")
        send_doc.assert_not_awaited()
        folder = self.customer(self.customer_id).drive_invoices_folder_id
        [file_id] = self.google.children(folder)
        day = datetime.now(timezone.utc).astimezone(drive_layout.IST).strftime("%Y-%m-%d")
        stored = self.google.files[file_id]
        self.assertEqual((stored["name"], stored["content"]), (f"{day} ACC-SINV-0001.pdf", PDF))
        recipient, text = send_text.await_args.args
        self.assertEqual(recipient, PHONE)
        self.assertIn("ACC-SINV-0001", text)
        self.assertIn(file_id, text)
        record = self.invoice_record()
        self.assertEqual((record.status, record.drive_file_id), ("sent", file_id))
        self.assertIn(file_id, record.drive_link)

    def test_a_drive_failure_sends_the_document_on_whatsapp_as_before(self):
        self.google.fail("POST", "/upload/drive/v3/files", 400, "badRequest")
        send_text = AsyncMock(return_value=True)
        with patch(SEND_TEXT, send_text):
            outcome, _svc, _local, send_doc = self.dispatch(500)
        self.assertEqual(outcome, "erpnext")
        send_text.assert_not_awaited()
        self.assertEqual(send_doc.await_args.kwargs["filename"], "ACC-SINV-0001.pdf")
        self.assertEqual(send_doc.await_args.kwargs["document_bytes"], PDF)
        self.assertIsNone(self.invoice_record().drive_file_id)

    def test_the_local_receipt_also_goes_to_drive(self):
        with patch(SEND_TEXT, AsyncMock(return_value=True)):
            outcome, _svc, _local, send_doc = self.dispatch(500, erp_enabled=False)
        self.assertEqual(outcome, "local")
        send_doc.assert_not_awaited()
        [file_id] = self.google.children(self.customer(self.customer_id).drive_invoices_folder_id)
        self.assertTrue(self.google.files[file_id]["name"].endswith(" Invoice_MoraaStudio_ACK1.pdf"))


# ── retention ──────────────────────────────────────────────────────────────────────────────────────────────

class DriveRetentionTests(Phase8Base):
    def test_images_older_than_90_days_are_deleted_and_newer_images_and_invoices_stay(self):
        old = asyncio.run(drive_layout.upload_delivery_image(self.customer_id, self.image("a.png"), "image/png"))
        new = asyncio.run(drive_layout.upload_delivery_image(self.customer_id, self.image("b.png"), "image/png"))
        invoice = asyncio.run(drive_layout.upload_invoice(self.customer_id, self.image("i.pdf"), "2026-01-01 INV-1.pdf"))
        old_order = self.order("m1", drive_file_id=old["file_id"], age=timedelta(days=91))
        new_order = self.order("m2", drive_file_id=new["file_id"], age=timedelta(days=89))
        with self.Session() as db:
            db.add(InvoiceRecord(payment_id="pay_old", customer_phone=PHONE, amount_rupees=500, status="sent",
                                 drive_file_id=invoice["file_id"], created_at=datetime.now(timezone.utc) - timedelta(days=400)))
            db.commit()
            self.assertEqual(dl.purge_drive_images(db), 1)
            self.assertEqual(dl.purge_drive_images(db), 0)
        self.assertNotIn(old["file_id"], self.google.files)
        self.assertIn(new["file_id"], self.google.files)
        self.assertIn(invoice["file_id"], self.google.files)
        with self.Session() as db:
            self.assertIsNone(db.get(WhatsAppIngestion, old_order).drive_file_id)
            self.assertEqual(db.get(WhatsAppIngestion, new_order).drive_file_id, new["file_id"])
            self.assertEqual(db.query(InvoiceRecord).one().drive_file_id, invoice["file_id"])

    def test_the_retention_pass_reports_drive_images(self):
        with patch.object(settings, "RETENTION_ENABLED", True), patch.object(dl, "purge_drive_images", return_value=2):
            self.assertEqual(dl.run_retention_pass()["drive_images"], 2)
        with patch.object(settings, "RETENTION_ENABLED", False):
            self.assertEqual(dl.run_retention_pass()["drive_images"], 0)


# ── erasure ────────────────────────────────────────────────────────────────────────────────────────────────

class ErasureTests(Phase8Base):
    def test_erasure_is_refused_while_credits_remain(self):
        for sku in (SKU_WHITE_BG, SKU_CREATIVE):
            with self.Session() as db:
                db.query(CustomerSkuCredit).delete()
                db.commit()
            self.grant(3, sku)
            with self.assertRaises(dl.ErasureRefused) as refused:
                dl.erase_customer_by_id(self.customer_id)
            self.assertIn("3 unused image credits", str(refused.exception))
        self.assertEqual(self.customer(self.customer_id).whatsapp_id, PHONE)

    def test_erasure_is_refused_while_an_image_waits_for_drive(self):
        self.order("m1", status="drive_pending")
        with self.assertRaises(dl.ErasureRefused):
            dl.erase_customer_by_id(self.customer_id)

    def test_erasure_cleans_the_drive_folder_and_keeps_invoices(self):
        with patch(SEND_TEXT, AsyncMock(return_value=True)):
            asyncio.run(drive_layout.share_customer_folder(self.customer_id))
        image = asyncio.run(drive_layout.upload_delivery_image(self.customer_id, self.image(), "image/png"))
        invoice = asyncio.run(drive_layout.upload_invoice(self.customer_id, self.image("i.pdf"), "2026-10-07 INV-1.pdf"))
        with self.Session() as db:
            db.add(InvoiceRecord(payment_id="pay_kept", customer_phone=PHONE, amount_rupees=500, status="sent",
                                 drive_file_id=invoice["file_id"]))
            db.commit()
        before = self.customer(self.customer_id)
        self.assertEqual(before.drive_shared_to, "buyer@gmail.com")
        result = dl.erase_customer_by_id(self.customer_id)
        self.assertEqual(result["drive"], 1)
        self.assertNotIn(image["file_id"], self.google.files)
        self.assertIn(invoice["file_id"], self.google.files)
        self.assertEqual(self.google.files[before.drive_folder_id]["name"], f"erased-{self.customer_id[:8]}")
        self.assertEqual(self.google.permissions[before.drive_folder_id], [])
        after = self.customer(self.customer_id)
        self.assertTrue(after.whatsapp_id.startswith("erased-"))
        self.assertIsNone(after.drive_shared_to)
        with self.Session() as db:
            self.assertEqual(db.query(InvoiceRecord).one().drive_file_id, invoice["file_id"])

    def test_a_drive_failure_never_blocks_the_database_erasure(self):
        asyncio.run(drive_layout.ensure_customer_folders(self.customer_id))
        self.google.fail("DELETE", "/drive/v3/files/", 400, "badRequest")
        result = dl.erase_customer_by_id(self.customer_id)
        self.assertEqual(result["drive"], 0)
        self.assertTrue(self.customer(self.customer_id).whatsapp_id.startswith("erased-"))

    def test_a_customer_without_a_drive_folder_never_calls_drive(self):
        with patch.object(drive_layout, "erase_customer_drive", AsyncMock()) as erase:
            self.assertEqual(dl.erase_customer_by_id(self.customer_id)["drive"], 0)
        erase.assert_not_awaited()


# ── dashboard and alerts ───────────────────────────────────────────────────────────────────────────────────

class DashboardAndAlertTests(Phase8Base):
    def test_profile_shows_credits_and_the_drive_folder(self):
        self.grant(20)
        self.grant(2, SKU_CREATIVE)
        with self.Session() as db:
            profile = dashboard_service.profile(db, PHONE)
        self.assertEqual(profile["sku_credits"], {"white_bg": 20, "creative_pack": 2})
        self.assertIsNone(profile["drive_folder"])
        root = asyncio.run(drive_layout.ensure_customer_folders(self.customer_id))["root"]
        with self.Session() as db:
            self.assertEqual(dashboard_service.profile(db, PHONE)["drive_folder"],
                             f"https://drive.google.com/drive/folders/{root}")

    def test_stats_count_packs_revenue_and_outstanding_credits(self):
        self.grant(20)
        self.grant(2, SKU_CREATIVE)
        with self.Session() as db:
            for payment_id, details in (
                ("pay_A", {"amount_paid": 400, "purpose": "sku_pack", "units": 20}),
                ("pay_B", {"amount_paid": 200, "purpose": "sku_pack", "units": 0, "creative_packs": 2}),
                ("pay_C", {"amount_paid": 500}),                                    # a wallet recharge
            ):
                db.add(AuditLog(action="razorpay_payment_captured", resource_id=payment_id, status="success",
                                resource_type="razorpay_payment", details=json.dumps(details)))
            db.commit()
            stats = dashboard_service.sku_stats(db)
        self.assertEqual(stats, {"packs_sold": 2, "pack_revenue": 600,
                                 "credits_outstanding": {"white_bg": 20, "creative_pack": 2}})

    def test_drive_alerts(self):
        with self.Session() as db:
            self.assertFalse({"drive_fallbacks", "drive_pending_stuck"} & {a.key for a in alert_service.evaluate_alerts(db)})
        self.order("m1", status="drive_pending", age=timedelta(hours=4))
        self.order("m2", status="drive_pending", age=timedelta(hours=1))
        fallback = self.order("m3", channel="whatsapp")
        with self.Session() as db:
            db.add(OutboxJob(kind="drive_deliver", dedupe_key=f"drive:deliver:{fallback}", payload={}, status="done"))
            db.add(OutboxJob(kind="drive_deliver", dedupe_key="drive:deliver:gone", payload={}, status=DEAD))
            db.commit()
            alerts = {a.key: a.message for a in alert_service.evaluate_alerts(db)}
        self.assertIn("1 finished image(s)", alerts["drive_pending_stuck"])
        self.assertIn("1 image(s) meant for Google Drive", alerts["drive_fallbacks"])
        self.assertIn("1 Drive upload job(s) gave up", alerts["drive_fallbacks"])


if __name__ == "__main__":
    unittest.main()
