"""Ecommerce Shot Only (₹50) + E-Com Pack 1 (₹500) — product-choice tests.

Real WhatsApp order of events exercised over HTTP:
photo webhook -> stored (awaiting_choice, no charge) -> 2 reply buttons ->
button_reply webhook -> guarded claim -> atomic debit -> the right worker.
All Meta / AI calls are mocked: no network, no credits.
"""

import asyncio
import hashlib
import json
import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

from app.api.dependencies import require_auth
from app.models.audit_log import AuditLog
from app.models.image import Image
from app.models.whatsapp_ingestion import PRODUCT_PACK_1, PRODUCT_WHITE_BG, WhatsAppIngestion
from app.repositories.base import BaseRepository
from app.services import meta_whatsapp_service as mws
from app.services import wallet_service
from app.services.ecommerce_shot_prompt import build_ecommerce_shot_prompt
from tests.test_wallet_funded_slot_gate import (
    SENDER,
    FundedSlotGateTestCase,
    _make_customer,
    _make_engine_and_session,
)

WHITE = 50
PACK = wallet_service.price_per_image()
IMAGE_BYTES = b"\xff\xd8\xff" + b"\x00" * 64  # what the harness download mock returns


def _image(message_id="wamid.img.1", caption=None, sender=SENDER):
    image = {"id": "media_1", "mime_type": "image/jpeg"}
    if caption is not None:
        image["caption"] = caption
    return {"object": "whatsapp_business_account", "entry": [{"changes": [{"field": "messages", "value": {
        "messages": [{"type": "image", "id": message_id, "from": sender, "timestamp": "1700000000", "image": image}]}}]}]}


def _tap(button_id, message_id="wamid.tap.1", sender=SENDER):
    return {"object": "whatsapp_business_account", "entry": [{"changes": [{"field": "messages", "value": {
        "messages": [{"type": "interactive", "id": message_id, "from": sender, "timestamp": "1700000001",
                      "context": {"from": "15550000000", "id": "wamid.buttons"},
                      "interactive": {"type": "button_reply", "button_reply": {"id": button_id, "title": "x"}}}]}}]}]}


class ButtonIdTests(unittest.TestCase):
    def test_round_trip(self):
        self.assertEqual(mws.parse_product_button_id("gv_white:abc"), ("gv_white", "abc"))
        self.assertEqual(mws.parse_product_button_id("gv_pack1:abc"), ("gv_pack1", "abc"))

    def test_rejects_other_ids(self):
        for bad in ("feedback_positive_x", "gv_white:", "gv_other:abc", "prompt_ecommerce:abc", "", None):
            self.assertIsNone(mws.parse_product_button_id(bad), bad)

    def test_meta_parser_exposes_button_id(self):
        events = mws.parse_webhook_entry(_tap("gv_white:ing-1")["entry"][0])
        self.assertEqual(events[0]["type"], "interactive")
        self.assertEqual(events[0]["button_reply"]["id"], "gv_white:ing-1")
        self.assertEqual(events[0]["sender"], SENDER)


class ButtonPayloadTests(unittest.TestCase):
    def test_meta_interactive_payload(self):
        captured = {}

        async def fake_post(payload, label, reply_to_message_id=None):
            captured.update(payload=payload, reply=reply_to_message_id)
            return True

        with patch.object(mws, "_post_message_payload", new=fake_post):
            ok = asyncio.run(mws.send_product_selection_buttons(SENDER, "ing-1", 50, 500, 700, "wamid.img.1"))
        self.assertTrue(ok)
        p = captured["payload"]
        self.assertEqual(p["type"], "interactive")
        self.assertEqual(p["to"], SENDER)
        self.assertEqual(p["interactive"]["type"], "button")
        self.assertIn("What would you like to create?", p["interactive"]["body"]["text"])
        self.assertIn("Ecommerce Shot Only — ₹50", p["interactive"]["body"]["text"])
        self.assertIn("E-Com Pack 1 — ₹500", p["interactive"]["body"]["text"])
        buttons = [b["reply"] for b in p["interactive"]["action"]["buttons"]]
        self.assertEqual([b["id"] for b in buttons], ["gv_white:ing-1", "gv_pack1:ing-1"])
        self.assertEqual([b["title"] for b in buttons], ["Ecommerce Shot ₹50", "E-Com Pack 1 ₹500"])
        for b in buttons:
            self.assertLessEqual(len(b["title"]), 20)  # Meta reply-button title limit
            self.assertLessEqual(len(b["id"]), 256)
        self.assertEqual(captured["reply"], "wamid.img.1")


class ProductChoiceWebhookTests(FundedSlotGateTestCase):
    def setUp(self):
        super().setUp()
        self.white_worker = AsyncMock(return_value=True)
        self.extra = [
            patch.object(self.webhook_module, "process_whatsapp_white_bg", new=self.white_worker),
            patch.object(mws, "DRY_RUN_IMAGE_MODE", False),
        ]
        for p in self.extra:
            p.start()
        self.buttons = self.webhook_module.send_product_selection_buttons
        self.pack_worker = self.webhook_module.process_whatsapp_catalog_pack

    def tearDown(self):
        for p in reversed(self.extra):
            p.stop()
        super().tearDown()

    def _post(self, payload):
        r = self.client.post("/api/meta/webhook", json=payload)
        self.assertEqual(r.status_code, 200)
        return r.json()

    def _rows(self):
        return self.session.query(WhatsAppIngestion).order_by(WhatsAppIngestion.external_message_id).all()

    def _upload(self, message_id="wamid.img.1", **kw):
        self._post(_image(message_id, **kw))
        row = self.session.query(WhatsAppIngestion).filter_by(external_message_id=message_id).first()
        self.session.refresh(row)
        return row

    def _texts(self):
        return [t or "" for t in self.sent_texts]

    # ── Upload: stored, buttons, NO charge ──
    def test_upload_stores_photo_sends_buttons_and_charges_nothing(self):
        _make_customer(self.session, balance=700)
        row = self._upload(caption="white")  # caption must NOT select a product
        self.assertEqual(row.status, "awaiting_choice")
        self.assertIsNone(row.product_code)
        self.assertEqual(row.image_id, "image-record-1")
        self.assertEqual(self._balance(), 700)
        self.buttons.assert_awaited_once()
        kwargs = self.buttons.await_args.kwargs
        self.assertEqual((kwargs["ingestion_id"], kwargs["white_price"], kwargs["pack_price"], kwargs["balance"]),
                         (row.id, WHITE, PACK, 700))
        self.white_worker.assert_not_awaited()
        self.pack_worker.assert_not_called()

    # ── ₹50 ──
    def test_white_tap_charges_50_and_queues_white_worker_only(self):
        _make_customer(self.session, balance=700)
        row = self._upload()
        self._post(_tap(f"gv_white:{row.id}"))
        self.session.refresh(row)
        self.assertEqual((row.product_code, row.amount_charged, row.status), (PRODUCT_WHITE_BG, WHITE, "white_queued"))
        self.assertEqual(self._balance(), 650)
        self.white_worker.assert_awaited_once_with(row.id)
        self.pack_worker.assert_not_called()

    # ── ₹500 ──
    def test_pack_tap_charges_500_and_queues_existing_pack_worker_only(self):
        _make_customer(self.session, balance=700)
        row = self._upload()
        self._post(_tap(f"gv_pack1:{row.id}"))
        self.session.refresh(row)
        self.assertEqual((row.product_code, row.amount_charged, row.status), (PRODUCT_PACK_1, PACK, "pack_queued"))
        self.assertEqual(self._balance(), 200)
        self.pack_worker.assert_called_once_with(row.id)
        self.white_worker.assert_not_awaited()
        self.assertIn(mws.CATALOG_PACK_ACK_TEMPLATE, self._texts())

    # ── Balances ──
    def test_balance_0_and_49_cannot_upload_and_see_real_balance(self):
        for bal in (0, 49):
            with self.subTest(balance=bal):
                self.session.query(WhatsAppIngestion).delete()
                from app.models.customer import Customer
                self.session.query(Customer).delete()
                self.session.commit()
                self.sent_texts.clear()
                self.buttons.reset_mock()
                _make_customer(self.session, balance=bal)
                row = self._upload(message_id=f"wamid.b{bal}")
                self.assertEqual(row.status, "unfunded")
                self.buttons.assert_not_awaited()
                self.assertEqual(self._balance(), bal)
                self.assertTrue(any(f"Your wallet balance is ₹{bal}" in t for t in self._texts()))

    def test_balance_50_white_ok(self):
        _make_customer(self.session, balance=50)
        row = self._upload()
        self._post(_tap(f"gv_white:{row.id}"))
        self.assertEqual(self._balance(), 0)
        self.white_worker.assert_awaited_once()

    def test_balance_499_pack_declined_no_worker_then_white_ok(self):
        _make_customer(self.session, balance=499)
        row = self._upload()
        self._post(_tap(f"gv_pack1:{row.id}", message_id="wamid.t1"))
        self.session.refresh(row)
        self.assertEqual((row.status, row.product_code), ("awaiting_choice", None))
        self.assertEqual(self._balance(), 499)
        self.pack_worker.assert_not_called()
        self.assertTrue(any("Your wallet balance is ₹499" in t and "₹500 is required for E-Com Pack 1" in t
                            for t in self._texts()))
        self._post(_tap(f"gv_white:{row.id}", message_id="wamid.t2"))
        self.assertEqual(self._balance(), 449)
        self.white_worker.assert_awaited_once()

    def test_balance_500_pack_ok(self):
        _make_customer(self.session, balance=500)
        row = self._upload()
        self._post(_tap(f"gv_pack1:{row.id}"))
        self.assertEqual(self._balance(), 0)
        self.pack_worker.assert_called_once()

    def test_declined_then_recharge_then_tap_again_works(self):
        _make_customer(self.session, balance=100)
        row = self._upload()
        self._post(_tap(f"gv_pack1:{row.id}", message_id="wamid.t1"))
        wallet_service.credit_wallet(self.session, SENDER, 500)
        self._post(_tap(f"gv_pack1:{row.id}", message_id="wamid.t2"))
        self.assertEqual(self._balance(), 100)
        self.pack_worker.assert_called_once()

    # ── Idempotency ──
    def test_double_tap_and_duplicate_callback_charge_once(self):
        _make_customer(self.session, balance=700)
        row = self._upload()
        tap = _tap(f"gv_white:{row.id}")
        self._post(tap)
        self._post(tap)                                         # Meta redelivers the same callback
        self._post(_tap(f"gv_white:{row.id}", message_id="wamid.tap.2"))  # customer double-taps
        self._post(_tap(f"gv_pack1:{row.id}", message_id="wamid.tap.3"))  # then taps the other product
        self.assertEqual(self._balance(), 650)
        self.white_worker.assert_awaited_once()
        self.pack_worker.assert_not_called()
        self.assertTrue(any(self.webhook_module.ALREADY_CHOSEN_MESSAGE == t for t in self._texts()))

    def test_concurrent_tap_that_lost_the_claim_never_charges(self):
        _make_customer(self.session, balance=700)
        row = self._upload()
        row.status = "choice_claimed"  # another copy of the callback claimed it first
        self.session.commit()
        self._post(_tap(f"gv_white:{row.id}"))
        self.assertEqual(self._balance(), 700)
        self.white_worker.assert_not_awaited()

    def test_duplicate_image_webhook_stores_once_and_sends_buttons_once(self):
        _make_customer(self.session, balance=700)
        self._post(_image("wamid.dup"))
        self._post(_image("wamid.dup"))
        self.assertEqual(len(self._rows()), 1)
        self.buttons.assert_awaited_once()

    def test_concurrent_image_copy_loses_unique_claim(self):
        _make_customer(self.session, balance=700)
        self._post(_image("wamid.dup"))
        with patch.object(BaseRepository, "find_first", return_value=None):  # both copies pass the quick check
            self._post(_image("wamid.dup"))
        self.assertEqual(len(self._rows()), 1)
        self.buttons.assert_awaited_once()

    def test_redelivered_photo_after_paying_sends_no_zero_balance_message(self):
        # Regression for the real test: ₹500 wallet, photo, Pack 1, then Meta
        # redelivers the photo webhook -> must NOT say "Your balance is ₹0".
        _make_customer(self.session, balance=500)
        row = self._upload("wamid.real")
        self._post(_tap(f"gv_pack1:{row.id}"))
        before = list(self._texts())
        self._post(_image("wamid.real"))
        self.assertEqual(self._texts(), before)
        self.assertFalse(any("balance is ₹0" in t for t in self._texts()))
        self.pack_worker.assert_called_once()

    def test_second_photo_before_choosing_first_is_independent(self):
        _make_customer(self.session, balance=700)
        first = self._upload("wamid.a")
        second = self._upload("wamid.b")
        self._post(_tap(f"gv_white:{second.id}"))
        self.session.refresh(first)
        self.session.refresh(second)
        self.assertEqual(first.status, "awaiting_choice")
        self.assertEqual(second.status, "white_queued")
        self.white_worker.assert_awaited_once_with(second.id)
        self.assertEqual(self.buttons.await_count, 2)

    def test_delayed_tap_uses_database_only(self):
        # Row written earlier (e.g. before a restart); nothing in memory.
        _make_customer(self.session, balance=700)
        row = WhatsAppIngestion(external_user_id=SENDER, external_message_id="wamid.old", channel="whatsapp",
                                image_id="image-record-1", status="awaiting_choice")
        self.session.add(row)
        self.session.commit()
        self._post(_tap(f"gv_white:{row.id}"))
        self.white_worker.assert_awaited_once_with(row.id)

    def test_foreign_or_unknown_photo_is_refused(self):
        _make_customer(self.session, balance=700)
        row = self._upload()
        self._post(_tap(f"gv_white:{row.id}", sender="919999999999"))
        self._post(_tap("gv_white:does-not-exist"))
        self.assertEqual(self._balance(), 700)
        self.white_worker.assert_not_awaited()

    def test_invalid_image_is_rejected_without_buttons(self):
        _make_customer(self.session, balance=700)
        with patch.object(self.webhook_module, "validate_image", return_value=(False, "bad")):
            row = self._upload()
        self.assertEqual(row.status, "rejected")
        self.buttons.assert_not_awaited()
        self.assertIn(self.webhook_module.UNREADABLE_IMAGE_MESSAGE, self._texts())

    def test_text_white_is_unchanged(self):
        _make_customer(self.session, balance=700)
        payload = {"object": "whatsapp_business_account", "entry": [{"changes": [{"field": "messages", "value": {
            "messages": [{"type": "text", "id": "wamid.t", "from": SENDER, "timestamp": "1", "text": {"body": "white"}}]}}]}]}
        self._post(payload)
        self.assertEqual(self._rows(), [])
        self.assertEqual(self.sent_texts, [])

    # ── DRY_RUN ──
    def test_white_dry_run_never_debits(self):
        _make_customer(self.session, balance=700)
        row = self._upload()
        with patch.object(mws, "DRY_RUN_IMAGE_MODE", True):
            self._post(_tap(f"gv_white:{row.id}"))
        self.session.refresh(row)
        self.assertEqual(row.amount_charged, 0)
        self.assertEqual(self._balance(), 700)
        self.white_worker.assert_awaited_once()

    # ── Retry dispatch ──
    def _retry(self, row):
        self.app.dependency_overrides[require_auth] = lambda: object()
        with patch.object(self.webhook_module, "_trigger_generation", new=AsyncMock()) as pack_retry:
            r = self.client.post(f"/api/meta/webhook/retry/{row.id}")
        self.assertEqual(r.status_code, 200)
        return pack_retry, r.json()

    def _row(self, **kw):
        row = WhatsAppIngestion(external_user_id=SENDER, external_message_id=kw.pop("mid", "wamid.r"),
                                channel="whatsapp", **kw)
        self.session.add(row)
        self.session.commit()
        return row

    def test_retry_white_goes_to_white_worker(self):
        row = self._row(status="failed", product_code=PRODUCT_WHITE_BG, amount_charged=WHITE)
        pack_retry, _ = self._retry(row)
        self.white_worker.assert_awaited_once_with(row.id)
        pack_retry.assert_not_awaited()

    def test_retry_legacy_null_goes_to_pack_1(self):
        row = self._row(status="failed")
        pack_retry, _ = self._retry(row)
        pack_retry.assert_awaited_once_with(row.id)
        self.white_worker.assert_not_awaited()

    def test_stuck_white_can_be_requeued_only_after_10_minutes(self):
        fresh = self._row(mid="wamid.s1", status="processing", product_code=PRODUCT_WHITE_BG, amount_charged=WHITE)
        _, body = self._retry(fresh)
        self.assertEqual(body["status"], "error")
        old = self._row(mid="wamid.s2", status="white_queued", product_code=PRODUCT_WHITE_BG, amount_charged=WHITE)
        old.updated_at = datetime.now(timezone.utc) - timedelta(minutes=30)
        self.session.commit()
        self.session.query(WhatsAppIngestion).filter_by(id=old.id).update(
            {"updated_at": datetime.now(timezone.utc) - timedelta(minutes=30)}, synchronize_session=False)
        self.session.commit()
        _, body = self._retry(old)
        self.assertEqual(body["status"], "queued")


class WhiteWorkerTests(unittest.TestCase):
    def setUp(self):
        self.engine, self.db = _make_engine_and_session()
        _make_customer(self.db, balance=650)  # ₹50 already debited from 700
        self.dir = tempfile.mkdtemp()
        self.path = os.path.join(self.dir, "x.jpg")
        Path(self.path).write_bytes(IMAGE_BYTES)
        img = Image(original_filename="x.jpg", stored_filename="x.jpg", file_path=self.path,
                    file_size=len(IMAGE_BYTES), mime_type="image/jpeg", image_url="/x.jpg")
        self.db.add(img)
        self.db.commit()
        self.ingestion = WhatsAppIngestion(
            external_user_id=SENDER, external_message_id="wamid.worker", external_media_id="m1",
            channel="whatsapp", image_id=img.id, status="white_queued", mime_type="image/jpeg",
            product_code=PRODUCT_WHITE_BG, amount_charged=WHITE,
        )
        self.db.add(self.ingestion)
        self.db.commit()
        self.gen = AsyncMock(return_value=MagicMock(
            success=True, image_url="data:image/png;base64,AAAA", error=None,
            provider_name="gemini", model_used="gemini-3.1-flash-image", fallback_used=False))
        manager = MagicMock()
        manager.generate_image = self.gen
        self.upload = AsyncMock(return_value="media-1")
        self.send = AsyncMock(return_value=True)
        self.text = AsyncMock(return_value=True)
        self.patches = [
            patch("app.database.SessionLocal", return_value=self.db),
            patch.object(self.db, "close"),
            patch.object(mws, "DRY_RUN_IMAGE_MODE", False),
            patch("app.ai.image_generation_manager.ImageGenerationManager", return_value=manager),
            patch.object(mws, "upload_media_to_meta", new=self.upload),
            patch.object(mws, "send_image_to_whatsapp", new=self.send),
            patch.object(mws, "send_whatsapp_text", new=self.text),
        ]
        for p in self.patches:
            p.start()

    def tearDown(self):
        for p in reversed(self.patches):
            p.stop()
        self.db.close()
        self.engine.dispose()

    def _run(self):
        return asyncio.run(mws.process_whatsapp_white_bg(self.ingestion.id))

    def _balance(self):
        return wallet_service.get_balance(self.db, SENDER)

    def _refunds(self):
        return self.db.query(AuditLog).filter(AuditLog.action == mws.REFUND_AUDIT_ACTION).count()

    def test_success_one_image_ecommerce_shot_prompt_stored_and_recorded(self):
        self.assertTrue(self._run())
        self.db.refresh(self.ingestion)
        self.assertEqual(self.gen.await_count, 1)
        self.assertEqual(self.gen.await_args.kwargs["prompt"], build_ecommerce_shot_prompt())
        self.assertEqual(self.gen.await_args.kwargs["context"]["aspect_ratio"], "1:1")
        self.assertEqual(self.gen.await_args.kwargs["reference_image"], IMAGE_BYTES)
        self.assertEqual((self.upload.await_count, self.send.await_count), (1, 1))
        self.assertEqual(self.ingestion.status, "delivered")
        self.assertEqual(self._balance(), 650)
        self.assertEqual(self._refunds(), 0)
        audit = self.db.query(AuditLog).filter(AuditLog.action == mws.WHITE_BG_AUDIT_ACTION).one()
        details = json.loads(audit.details)
        self.assertEqual((details["provider"], details["model"]), ("gemini", "gemini-3.1-flash-image"))
        self.assertTrue(Path(details["output_path"]).exists())

    def test_generation_failure_refunds_50_once_and_tells_customer(self):
        self.gen.return_value = MagicMock(success=False, image_url=None, error="boom")
        self.assertFalse(self._run())
        self.db.refresh(self.ingestion)
        self.assertEqual(self.ingestion.status, "failed")
        self.ingestion.status = "stored"  # what the retry endpoint does
        self.db.commit()
        self.assertFalse(self._run())
        self.assertEqual(self._balance(), 700)
        self.assertEqual(self._refunds(), 1)
        self.assertIn("₹50 has been refunded", self.text.await_args_list[0].args[1])

    def test_delivery_failure_refunds_once(self):
        self.send.return_value = False
        self.assertFalse(self._run())
        self.db.refresh(self.ingestion)
        self.assertEqual(self.ingestion.status, "delivery_failed")
        self.assertEqual(self._balance(), 700)
        self.assertEqual(self._refunds(), 1)

    def test_unexpected_exception_marks_failed_and_refunds_once(self):
        self.upload.side_effect = RuntimeError("network exploded")
        self.assertFalse(self._run())
        self.db.refresh(self.ingestion)
        self.assertEqual(self.ingestion.status, "failed")
        self.assertEqual(self._balance(), 700)
        self.assertEqual(self._refunds(), 1)

    def test_second_trigger_cannot_claim_again(self):
        self.assertTrue(self._run())
        self.assertFalse(self._run())  # already delivered: guarded claim fails
        self.assertEqual(self.gen.await_count, 1)

    def test_refuses_pack_1_and_unpaid_rows(self):
        for product, status in ((PRODUCT_PACK_1, "white_queued"), (PRODUCT_WHITE_BG, "awaiting_choice"),
                                (PRODUCT_WHITE_BG, "choice_claimed"), (PRODUCT_WHITE_BG, "processing")):
            self.ingestion.product_code, self.ingestion.status = product, status
            self.db.commit()
            self.assertFalse(self._run())
        self.gen.assert_not_awaited()

    def test_dry_run_makes_no_generation_call(self):
        self.ingestion.amount_charged = 0
        self.db.commit()
        with patch.object(mws, "DRY_RUN_IMAGE_MODE", True):
            self.assertTrue(self._run())
        self.gen.assert_not_awaited()
        self.assertEqual(self.send.await_args.kwargs["caption"], mws.WHITE_BG_DRY_RUN_CAPTION)

    def test_legacy_pack_1_refund_still_uses_pack_price(self):
        legacy = WhatsAppIngestion(external_user_id=SENDER, external_message_id="wamid.legacy",
                                   channel="whatsapp", status="processing")
        self.db.add(legacy)
        self.db.commit()
        mws._fail_ingestion(self.db, legacy, "legacy failure")
        self.assertEqual(self._balance(), 650 + PACK)


class Prompt1UnchangedTests(unittest.TestCase):
    def test_prompt_1_source_matches_head(self):
        path = Path(__file__).resolve().parents[1] / "app/services/earring_ecommerce_prompt.py"
        digest = hashlib.md5(path.read_bytes().replace(b"\r\n", b"\n")).hexdigest()
        self.assertEqual(digest, "da7c0737327102ce6a0868bfa1f6bf4b")


if __name__ == "__main__":
    unittest.main()
