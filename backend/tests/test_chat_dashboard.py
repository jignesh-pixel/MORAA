"""The WhatsApp chat dashboard: recording and the read-only API."""

import tempfile
import time
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from fastapi.testclient import TestClient

from app.api.dependencies import require_auth
from app.config import settings
from app.database import get_db
from app.main import app
from app.models.chat_log import ChatMessage, InvoiceRecord, OrderOutput
from app.models.image import Image
from app.models.wallet_transaction import KIND_CREDIT_PAYMENT, WalletTransaction
from app.models.whatsapp_ingestion import WhatsAppIngestion
from app.services import chat_log, dashboard_service as svc
from tests.test_wallet_ledger import SENDER, LedgerTestBase


class _Base(LedgerTestBase):
    def setUp(self):
        super().setUp()
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        for p in (patch("app.database.SessionLocal", return_value=self.db), patch.object(self.db, "close"),
                  patch.object(type(settings), "UPLOAD_PATH", new=property(lambda s: Path(self.tmp.name))),
                  patch.object(settings, "CHAT_LOG_ENABLED", True)):
            p.start()
            self.addCleanup(p.stop)
        chat_log.reset_for_tests()

    def msg(self, direction="in", text="hi", minutes_ago=0, **extra):
        row = ChatMessage(customer_phone=SENDER, direction=direction, msg_type=extra.pop("msg_type", "text"), text=text, **extra)
        row.created_at = datetime.now(timezone.utc) - timedelta(minutes=minutes_ago)
        self.db.add(row)
        self.db.commit()
        return row


class RecordingTests(_Base):
    def test_inbound_text_button_and_photo_events_are_recorded_once(self):
        chat_log.write_in({"type": "text", "sender": SENDER, "message_id": "w1", "body": "hello"})
        chat_log.write_in({"type": "text", "sender": SENDER, "message_id": "w1", "body": "hello"})      # Meta re-delivery
        chat_log.write_in({"type": "interactive", "subtype": "button_reply", "sender": SENDER, "message_id": "w2",
                           "button_reply": {"id": "gv_white:1", "title": "Studio Shot"}})
        chat_log.write_in({"type": "image", "sender": SENDER, "message_id": "w3", "media_id": "m9", "caption": "ring"})
        chat_log.write_in({"type": "status", "message_id": "x"})                                        # delivery receipts are not chat
        rows = self.db.query(ChatMessage).order_by(ChatMessage.created_at).all()
        self.assertEqual([(r.direction, r.msg_type, r.text) for r in rows],
                         [("in", "text", "hello"), ("in", "button_reply", "Tapped: Studio Shot"), ("in", "image", "ring")])

    def test_outbound_messages_are_described_readably(self):
        cases = [
            ({"type": "text", "to": SENDER, "text": {"body": "Welcome"}}, ("text", "Welcome")),
            ({"type": "image", "to": SENDER, "image": {"id": "m1", "caption": "1/6 Style"}}, ("image", "1/6 Style")),
            ({"type": "document", "to": SENDER, "document": {"id": "d1", "filename": "inv.pdf", "caption": ""}}, ("document", "📄 inv.pdf")),
            ({"type": "interactive", "to": SENDER, "interactive": {"type": "button", "body": {"text": "Pick"},
              "action": {"buttons": [{"reply": {"title": "A"}}, {"reply": {"title": "B"}}]}}}, ("buttons", "Pick\n[ A | B ]")),
        ]
        for i, (payload, (kind, text)) in enumerate(cases):
            chat_log.write_out(payload, f"out{i}")
        rows = self.db.query(ChatMessage).filter(ChatMessage.direction == "out").order_by(ChatMessage.wa_message_id).all()
        self.assertEqual([(r.msg_type, r.text) for r in rows], [c[1] for c in cases])

    def test_a_sent_image_is_linked_to_the_output_we_kept(self):
        output_id = chat_log.save_output("ing-1", "Style A", 0, b"\x89PNG-bytes")
        chat_log.link_output_media(output_id, "meta-123")
        chat_log.write_out({"type": "image", "to": SENDER, "image": {"id": "meta-123", "caption": "x"}}, "o1")
        row = self.db.query(ChatMessage).one()
        self.assertEqual((row.output_id, row.ingestion_id), (output_id, "ing-1"))
        kept = self.db.get(OrderOutput, output_id)
        self.assertEqual((Path(self.tmp.name) / kept.file_path).read_bytes(), b"\x89PNG-bytes")

    def test_a_photo_is_attached_to_its_order_even_if_the_generic_record_is_late(self):
        chat_log.attach_photo("w9", SENDER, "ing-9", "img-9")
        chat_log.write_in({"type": "image", "sender": SENDER, "message_id": "w9", "media_id": "m", "caption": ""})
        row = self.db.query(ChatMessage).one()
        self.assertEqual((row.ingestion_id, row.image_id), ("ing-9", "img-9"))

    def test_a_missing_table_is_quiet_and_switches_the_writer_off_for_a_while(self):
        with patch.object(chat_log, "_session", side_effect=RuntimeError("no such table")):
            chat_log.write_in({"type": "text", "sender": SENDER, "message_id": "w1", "body": "x"})
        self.assertFalse(chat_log.enabled())

    def test_invoices_are_noted_and_updated(self):
        chat_log.upsert_invoice("pay_1", SENDER, 500, "failed")
        chat_log.upsert_invoice("pay_1", SENDER, 500, "sent", "ACC-SINV-1")
        row = self.db.query(InvoiceRecord).one()
        self.assertEqual((row.status, row.erpnext_invoice), ("sent", "ACC-SINV-1"))


class QueryTests(_Base):
    def test_customer_list_is_newest_first_with_a_preview_and_name(self):
        self.make_customer(0)
        self.msg("in", "old", minutes_ago=30)
        self.msg("out", "Welcome back", minutes_ago=1)
        other = ChatMessage(customer_phone="919000000009", direction="in", msg_type="text", text="hey")
        other.created_at = datetime.now(timezone.utc) - timedelta(minutes=10)
        self.db.add(other)
        self.db.commit()
        rows = svc.list_customers(self.db)
        self.assertEqual([r["phone"] for r in rows], [SENDER, "919000000009"])
        self.assertEqual((rows[0]["last_preview"], rows[0]["name"], rows[0]["messages"]), ("Welcome back", "T", 2))

    def test_search_by_name_and_by_number(self):
        self.make_customer(0)
        self.msg("in", "x")
        self.assertEqual(len(svc.list_customers(self.db, "5678")), 1)
        self.assertEqual(len(svc.list_customers(self.db, "T")), 1)
        self.assertEqual(svc.list_customers(self.db, "zzzz"), [])

    def test_messages_older_than_ninety_days_are_not_shown(self):
        self.msg("in", "ancient", minutes_ago=60 * 24 * 95)
        self.msg("in", "recent", minutes_ago=5)
        items = svc.timeline(self.db, SENDER)["items"]
        self.assertEqual([i["text"] for i in items], ["recent"])

    def test_timeline_merges_chat_payments_and_invoices_in_time_order(self):
        customer = self.make_customer(0)
        self.msg("in", "recharge 500", minutes_ago=20)
        tx = WalletTransaction(customer_id=customer.id, kind=KIND_CREDIT_PAYMENT, amount=500, balance_after=500, ref="pay_1")
        tx.created_at = datetime.now(timezone.utc) - timedelta(minutes=15)
        self.db.add(tx)
        inv = InvoiceRecord(payment_id="pay_1", customer_phone=SENDER, amount_rupees=500, status="sent", erpnext_invoice="ACC-1")
        inv.created_at = datetime.now(timezone.utc) - timedelta(minutes=10)
        self.db.add(inv)
        self.msg("out", "Thanks!", minutes_ago=5)
        self.db.commit()
        with patch.object(settings, "ERPNEXT_BASE_URL", "https://erp.example.com"):
            items = svc.timeline(self.db, SENDER)["items"]
        self.assertEqual([i["type"] for i in items], ["message", "event", "event", "message"])
        self.assertIn("₹500", items[1]["text"])
        self.assertEqual(items[2]["link"], "https://erp.example.com/app/sales-invoice/ACC-1")

    def test_image_messages_carry_signed_links(self):
        out = OrderOutput(ingestion_id="i1", style="S", position=1, file_path="outputs/i1/a.png", drive_link="https://drive/x")
        self.db.add(out)
        self.db.commit()
        self.msg("out", "pic", msg_type="image", output_id=out.id)
        media = svc.timeline(self.db, SENDER)["items"][0]["media"]
        self.assertTrue(media["url"].startswith(f"/api/dashboard/media/output/{out.id}?exp="))
        self.assertEqual(media["drive_link"], "https://drive/x")

    def test_profile_shows_details_payments_orders_and_invoices(self):
        customer = self.make_customer(0)
        customer.gst_number = "27AAAPS1234C1Z5"
        self.db.commit()
        tx = WalletTransaction(customer_id=customer.id, kind=KIND_CREDIT_PAYMENT, amount=1000, balance_after=1000, ref="pay_9")
        self.db.add(tx)
        order = self.make_ingestion(status="delivered")
        order.amount_charged = 50
        self.msg("in", "hi")
        self.db.commit()
        data = svc.profile(self.db, SENDER)
        self.assertEqual((data["name"], data["business"], data["gstin"]), ("T", "B", "27AAAPS1234C1Z5"))
        self.assertEqual(data["payments"][0]["amount"], 1000)
        self.assertEqual((data["orders"][0]["status"], data["orders"][0]["amount"]), ("delivered", 50))
        self.assertIsNone(svc.profile(self.db, "910000000000"))

    def test_audit_lists_input_and_outputs_and_can_filter_problems(self):
        self.make_customer(0)
        img = Image(request_id="r1", original_filename="a.jpg", stored_filename="a.jpg", file_path="x", file_size=1,
                    mime_type="image/jpeg", image_url="/u", processing_status="stored")
        self.db.add(img)
        self.db.commit()
        good = self.make_ingestion(status="delivered", message_id="a1", image_id=img.id)
        bad = self.make_ingestion(status="failed", message_id="a2", image_id=img.id)
        self.db.add(OrderOutput(ingestion_id=good.id, style="S", position=1, file_path="outputs/x.png"))
        self.db.commit()
        everything = svc.audit(self.db)
        self.assertEqual(len(everything), 2)
        only_bad = svc.audit(self.db, status="problems")
        self.assertEqual([o["id"] for o in only_bad], [bad.id])
        by_id = {o["id"]: o for o in everything}
        self.assertEqual(len(by_id[good.id]["outputs"]), 1)
        self.assertTrue(by_id[good.id]["input"]["url"].startswith("/api/dashboard/media/photo/"))


class MediaTests(_Base):
    def test_signature_expiry_and_tampering(self):
        url = svc.sign_media("output", "abc")
        exp = int(url.split("exp=")[1].split("&")[0])
        sig = url.split("sig=")[1]
        self.assertTrue(svc.media_signature_ok("output", "abc", exp, sig))
        self.assertFalse(svc.media_signature_ok("output", "abd", exp, sig))
        self.assertFalse(svc.media_signature_ok("photo", "abc", exp, sig))
        self.assertFalse(svc.media_signature_ok("output", "abc", int(time.time()) - 1, svc._mac("output", "abc", int(time.time()) - 1)))

    def test_only_files_inside_the_uploads_folder_are_served(self):
        inside = Path(self.tmp.name) / "outputs" / "i" / "a.png"
        inside.parent.mkdir(parents=True)
        inside.write_bytes(b"png")
        good = OrderOutput(ingestion_id="i", style="S", position=1, file_path="outputs/i/a.png")
        escape = OrderOutput(ingestion_id="i", style="S", position=2, file_path="../../etc/passwd")
        self.db.add_all([good, escape])
        self.db.commit()
        self.assertEqual(svc.resolve_media_file(self.db, "output", good.id)["path"], str(inside.resolve()))
        self.assertIsNone(svc.resolve_media_file(self.db, "output", escape.id))
        self.assertIsNone(svc.resolve_media_file(self.db, "output", "missing"))
        self.assertIsNone(svc.resolve_media_file(self.db, "other", good.id))


class ApiTests(_Base):
    def setUp(self):
        super().setUp()
        self.user = SimpleNamespace(id="u1", username="owner", email="owner@example.com", is_admin=True)
        app.dependency_overrides[get_db] = lambda: self.db
        app.dependency_overrides[require_auth] = lambda: self.user
        self.addCleanup(app.dependency_overrides.clear)
        p = patch.object(settings, "RATE_LIMIT_ENABLED", False)
        p.start()
        self.addCleanup(p.stop)
        self.client = TestClient(app, base_url="http://localhost")

    def test_an_administrator_can_read_and_an_ordinary_user_cannot(self):
        self.make_customer(0)
        self.msg("in", "hello")
        ok = self.client.get("/api/dashboard/customers")
        self.assertEqual((ok.status_code, ok.json()["customers"][0]["phone"]), (200, SENDER))
        self.user.is_admin = False
        with patch.object(settings, "ADMIN_USERNAMES", ""):
            self.assertEqual(self.client.get("/api/dashboard/customers").status_code, 403)
            self.assertEqual(self.client.get(f"/api/dashboard/customers/{SENDER}/timeline").status_code, 403)

    def test_nobody_is_let_in_just_because_no_administrator_is_configured(self):
        self.user.is_admin = False
        with patch.object(settings, "ADMIN_USERNAMES", ""), patch.object(settings, "DASHBOARD_ALLOWED_EMAILS", ""):
            self.assertEqual(self.client.get("/api/dashboard/me").status_code, 403)

    def test_an_allowed_email_can_read(self):
        self.user.is_admin = False
        self.user.is_verified = True
        with patch.object(settings, "ADMIN_USERNAMES", ""), patch.object(settings, "DASHBOARD_ALLOWED_EMAILS", "Owner@Example.com"):
            self.assertEqual(self.client.get("/api/dashboard/me").status_code, 200)

    def test_an_unverified_account_with_an_allowed_email_is_refused(self):
        # a password sign-up never proves it owns the email address
        self.user.is_admin = False
        self.user.is_verified = False
        with patch.object(settings, "ADMIN_USERNAMES", ""), patch.object(settings, "DASHBOARD_ALLOWED_EMAILS", "owner@example.com"):
            self.assertEqual(self.client.get("/api/dashboard/me").status_code, 403)

    def test_the_signed_image_link_works_without_a_login_and_a_bad_one_does_not(self):
        path = Path(self.tmp.name) / "outputs" / "i" / "a.png"
        path.parent.mkdir(parents=True)
        path.write_bytes(b"PNGDATA")
        out = OrderOutput(ingestion_id="i", style="S", position=1, file_path="outputs/i/a.png", mime_type="image/png")
        self.db.add(out)
        self.db.commit()
        app.dependency_overrides.pop(require_auth)
        good = self.client.get(svc.sign_media("output", out.id))
        self.assertEqual((good.status_code, good.content), (200, b"PNGDATA"))
        self.assertEqual(self.client.get(svc.sign_media("output", out.id).replace("sig=", "sig=0")).status_code, 403)

    def test_profile_404_for_an_unknown_number_and_the_dashboard_is_read_only(self):
        self.assertEqual(self.client.get("/api/dashboard/customers/910000000000/profile").status_code, 404)
        for method in ("post", "put", "delete", "patch"):
            self.assertEqual(getattr(self.client, method)("/api/dashboard/customers").status_code, 405)


if __name__ == "__main__":
    unittest.main()


class ReviewFixTests(_Base):
    def test_signed_addresses_are_stable_between_refreshes_and_still_expire(self):
        a, b = svc.sign_media("output", "x"), svc.sign_media("output", "x")
        self.assertEqual(a, b)
        exp = int(a.split("exp=")[1].split("&")[0])
        self.assertGreater(exp - time.time(), 600 - 1)             # at least 10 minutes of validity
        self.assertLessEqual(exp - time.time(), 900 + 300)

    def test_paging_back_never_skips_rows_when_one_source_is_sparse(self):
        customer = self.make_customer(0)
        for i in range(10):                                          # 10 messages, oldest first
            self.msg("in", f"m{i}", minutes_ago=100 - i * 10)
        tx = WalletTransaction(customer_id=customer.id, kind=KIND_CREDIT_PAYMENT, amount=500, balance_after=500, ref="p")
        tx.created_at = datetime.now(timezone.utc) - timedelta(minutes=200)       # an old event, older than every message
        self.db.add(tx)
        self.db.commit()
        seen = []
        before = None
        for _ in range(10):
            page = svc.timeline(self.db, SENDER, before=datetime.fromisoformat(before) if before else None, limit=4)
            seen += [i["text"] for i in page["items"]]
            if not page["has_more"]:
                break
            before = page["cursor"]
        texts = [t for t in seen if t and t.startswith("m")]
        self.assertEqual(sorted(texts), sorted(f"m{i}" for i in range(10)))      # every message exactly once
        self.assertEqual(len(texts), len(set(texts)))
        self.assertTrue(any("Payment received" in (t or "") for t in seen))

    def test_a_before_time_with_an_offset_is_read_as_that_moment(self):
        self.msg("in", "early", minutes_ago=120)
        self.msg("in", "late", minutes_ago=10)
        cut = (datetime.now(timezone.utc) - timedelta(minutes=60)).astimezone(timezone(timedelta(hours=5, minutes=30)))
        items = svc.timeline(self.db, SENDER, before=cut)["items"]
        self.assertEqual([i["text"] for i in items], ["early"])

    def test_an_invoice_link_is_only_built_from_a_web_address(self):
        with patch.object(settings, "ERPNEXT_BASE_URL", "javascript:alert(1)//"):
            self.assertIsNone(svc._invoice_url("INV-1"))
        with patch.object(settings, "ERPNEXT_BASE_URL", "https://erp.example.com/"):
            self.assertEqual(svc._invoice_url("A B/1"), "https://erp.example.com/app/sales-invoice/A%20B%2F1")

    def test_search_treats_percent_and_underscore_literally(self):
        self.make_customer(0)
        self.msg("in", "x")
        self.assertEqual(svc.list_customers(self.db, "%"), [])
        self.assertEqual(svc.list_customers(self.db, "_"), [])

    def test_one_failed_write_does_not_switch_recording_off(self):
        with patch.object(chat_log, "_session", side_effect=RuntimeError("temporary glitch")):
            chat_log.write_in({"type": "text", "sender": SENDER, "message_id": "g1", "body": "x"})
        self.assertTrue(chat_log.enabled())

    def test_the_photo_link_survives_the_generic_writer_getting_there_first(self):
        chat_log.write_in({"type": "image", "sender": SENDER, "message_id": "w5", "media_id": "m5", "caption": "c"})
        chat_log.attach_photo("w5", SENDER, "ing-5", "img-5")
        row = self.db.query(ChatMessage).one()
        self.assertEqual((row.ingestion_id, row.image_id, row.text, row.meta_media_id), ("ing-5", "img-5", "c", "m5"))

    def test_an_image_saved_after_the_message_was_recorded_is_connected_later(self):
        chat_log.write_out({"type": "image", "to": SENDER, "image": {"id": "late-1", "caption": "x"}}, "o9")   # message first
        output_id = chat_log.save_output("ing-7", "S", 0, b"\xff\xd8\xff-jpeg")
        chat_log.link_output_media(output_id, "late-1")
        row = self.db.query(ChatMessage).one()
        self.assertEqual((row.output_id, row.ingestion_id), (output_id, "ing-7"))
        self.assertEqual(self.db.get(OrderOutput, output_id).mime_type, "image/jpeg")

    def test_an_erased_number_is_not_recorded_again(self):
        chat_log.suppress(SENDER)
        chat_log.write_out({"type": "text", "to": SENDER, "text": {"body": "Done. Your data was deleted."}}, "z1")
        chat_log.write_in({"type": "text", "sender": SENDER, "message_id": "z2", "body": "bye"})
        self.assertEqual(self.db.query(ChatMessage).count(), 0)

    def test_the_invoice_record_survives_two_writers_racing(self):
        chat_log.upsert_invoice("pay_r", SENDER, 100, "failed")
        chat_log.upsert_invoice("pay_r", SENDER, 100, "sent", "ACC-9")
        self.assertEqual(self.db.query(InvoiceRecord).count(), 1)


class StartupCleanupTests(unittest.TestCase):
    def test_the_orphan_clean_up_never_deletes_the_images_we_produced(self):
        from app.utils.file_helpers import cleanup_orphaned_directories

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "outputs" / "i1").mkdir(parents=True)
            (root / "outputs" / "i1" / "a.png").write_bytes(b"x")
            (root / "stray-request").mkdir()
            (root / "known").mkdir()
            self.assertEqual(cleanup_orphaned_directories(root, {"known"}), 1)
            self.assertTrue((root / "outputs" / "i1" / "a.png").exists())
            self.assertFalse((root / "stray-request").exists())
