"""Tiered access (ADMIN / TRIAL / STANDARD) + complimentary trial credits.

Covers the entitlement helpers, the WhatsApp webhook (photo intake, product
button tap, registration confirmation) and the White BG / Pack 1 workers
(trial metering, exhaustion alert, admin daily-cap bypass). Every Meta / AI /
Razorpay call is mocked: no network, no credits.
"""

import asyncio
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.config import settings
from app.database import Base
from app.models.audit_log import AuditLog
from app.models.customer import Customer
from app.models.image import Image
from app.models.whatsapp_ingestion import PRODUCT_PACK_1, PRODUCT_WHITE_BG, WhatsAppIngestion
from app.services import entitlement_service as es
from app.services import meta_whatsapp_service as mws
from app.services import razorpay_service
from app.services import wallet_service
from tests.test_wallet_funded_slot_gate import SENDER, FundedSlotGateTestCase, _make_customer

WHITE = 50
PACK = wallet_service.price_per_image()
EXHAUSTED_PREFIX = (
    "You have used all your complimentary test credits ({n}/{n}). To continue generating "
    "studio-quality renders, recharge your wallet below:"
)
DATA_URL = "data:image/png;base64,QUJD"


def _cust(tier="STANDARD", total=0, used=0, shots=("all",), bypass=False):
    """Plain object for the pure helpers (no DB, no column defaults)."""
    return SimpleNamespace(
        tier=tier, trial_credits_total=total, trial_credits_used=used,
        allowed_shots=None if shots is None else list(shots), bypass_payment=bypass,
    )


def _set_tier(db, customer, tier="STANDARD", total=0, used=0, shots=("all",), bypass=False):
    customer.tier = tier
    customer.trial_credits_total = total
    customer.trial_credits_used = used
    customer.allowed_shots = list(shots)
    customer.bypass_payment = bypass
    db.commit()
    db.refresh(customer)
    return customer


def _body(call):
    return call.kwargs.get("body_text") or (call.args[1] if len(call.args) > 1 else "")


# ═══ 1. Pure helpers ═══════════════════════════════════════════════════════
class EntitlementHelperTests(unittest.TestCase):
    def test_settings_costs(self):
        self.assertEqual((settings.TRIAL_CREDITS_PER_WHITE_BG, settings.TRIAL_CREDITS_PER_PACK_1), (1, 1))

    def test_is_admin(self):
        self.assertTrue(es.is_admin(_cust("ADMIN")))
        self.assertTrue(es.is_admin(_cust("admin")))
        self.assertTrue(es.is_admin(_cust("STANDARD", bypass=True)))
        self.assertTrue(es.is_admin(_cust("TRIAL", total=3, bypass=True)))
        self.assertFalse(es.is_admin(_cust("STANDARD")))
        self.assertFalse(es.is_admin(_cust("TRIAL", total=3)))

    def test_is_trial(self):
        self.assertTrue(es.is_trial(_cust("TRIAL", total=1)))
        self.assertFalse(es.is_trial(_cust("TRIAL", total=1, bypass=True)))  # admin wins
        self.assertFalse(es.is_trial(_cust("STANDARD")))
        self.assertFalse(es.is_trial(_cust("ADMIN")))

    def test_trial_remaining_never_negative(self):
        self.assertEqual(es.trial_remaining(_cust("TRIAL", total=3, used=1)), 2)
        self.assertEqual(es.trial_remaining(_cust("TRIAL", total=3, used=3)), 0)
        self.assertEqual(es.trial_remaining(_cust("TRIAL", total=2, used=5)), 0)
        self.assertEqual(es.trial_remaining(_cust("TRIAL", total=0, used=0)), 0)

    def test_trial_shot_allowed(self):
        cases = [
            (["all"], True, True),
            (["white_bg"], True, False),
            (["pack_1"], False, True),
            (["white_bg", "pack_1"], True, True),
            ([], True, True),
            (None, True, True),
            (["WHITE_BG"], True, False),
            (["ALL"], True, True),
        ]
        for shots, white, pack in cases:
            with self.subTest(shots=shots):
                c = _cust("TRIAL", total=3, shots=shots)
                self.assertEqual(es.trial_shot_allowed(c, "WHITE_BG"), white)
                self.assertEqual(es.trial_shot_allowed(c, "PACK_1"), pack)

    def test_trial_can_use_and_has_credits(self):
        self.assertTrue(es.trial_can_use(_cust("TRIAL", total=1), "WHITE_BG"))
        self.assertTrue(es.trial_can_use(_cust("TRIAL", total=1), "PACK_1"))
        self.assertFalse(es.trial_can_use(_cust("TRIAL", total=1, used=1), "WHITE_BG"))
        self.assertFalse(es.trial_can_use(_cust("TRIAL", total=2, shots=["white_bg"]), "PACK_1"))
        self.assertFalse(es.trial_can_use(_cust("ADMIN", total=5), "WHITE_BG"))
        self.assertFalse(es.trial_can_use(_cust("STANDARD", total=5), "WHITE_BG"))
        self.assertTrue(es.has_trial_credits(_cust("TRIAL", total=2, used=1)))
        self.assertFalse(es.has_trial_credits(_cust("TRIAL", total=2, used=2)))
        self.assertFalse(es.has_trial_credits(_cust("STANDARD", total=2)))

    def test_payment_exempt(self):
        self.assertTrue(es.payment_exempt(_cust("ADMIN")))
        self.assertTrue(es.payment_exempt(_cust("STANDARD", bypass=True)))
        self.assertTrue(es.payment_exempt(_cust("TRIAL", total=1)))
        self.assertFalse(es.payment_exempt(_cust("TRIAL", total=1, used=1)))
        self.assertFalse(es.payment_exempt(_cust("STANDARD")))

    def test_record_trial_success_is_async(self):
        self.assertTrue(asyncio.iscoroutinefunction(es.record_trial_success))


class ModelDefaultTests(unittest.TestCase):
    def test_new_customer_defaults_to_standard(self):
        from tests.test_wallet_funded_slot_gate import _make_engine_and_session
        engine, db = _make_engine_and_session()
        try:
            c = _make_customer(db, balance=0)
            self.assertEqual(
                (c.tier, c.trial_credits_total, c.trial_credits_used, c.allowed_shots, c.bypass_payment),
                ("STANDARD", 0, 0, ["all"], False),
            )
        finally:
            db.close()
            engine.dispose()


# ═══ 2. Webhook: intake + tap ══════════════════════════════════════════════
def _image(message_id="wamid.img.1"):
    return {"object": "whatsapp_business_account", "entry": [{"changes": [{"field": "messages", "value": {
        "messages": [{"type": "image", "id": message_id, "from": SENDER, "timestamp": "1700000000",
                      "image": {"id": "media_1", "mime_type": "image/jpeg"}}]}}]}]}


def _tap(button_id, message_id="wamid.tap.1"):
    return {"object": "whatsapp_business_account", "entry": [{"changes": [{"field": "messages", "value": {
        "messages": [{"type": "interactive", "id": message_id, "from": SENDER, "timestamp": "1700000001",
                      "interactive": {"type": "button_reply", "button_reply": {"id": button_id, "title": "x"}}}]}}]}]}


class TierWebhookBase(FundedSlotGateTestCase):
    def setUp(self):
        super().setUp()
        self.white_worker = AsyncMock(return_value=True)
        self.cta = AsyncMock(return_value=True)

        async def capture_text(recipient_id, message_text=None, reply_to_message_id=None, **_):
            self.sent_texts.append(message_text)
            return True

        self.extra = [
            patch.object(self.webhook_module, "process_whatsapp_white_bg", new=self.white_worker),
            patch.object(self.webhook_module, "send_whatsapp_cta_url_button", new=self.cta),
            patch.object(self.webhook_module, "create_recharge_payment_link", new=AsyncMock(return_value="https://pay")),
            patch.object(mws, "send_whatsapp_text", new=AsyncMock(side_effect=capture_text)),
            patch.object(mws, "send_whatsapp_cta_url_button", new=self.cta),
            patch.object(mws, "send_reply_buttons", new=AsyncMock(return_value=True)),
            patch.object(razorpay_service, "create_recharge_payment_link", new=AsyncMock(return_value="https://pay")),
            patch.object(mws, "DRY_RUN_IMAGE_MODE", False),
            patch.object(settings, "WHATSAPP_PAY_ENABLED", False),
            patch.object(settings, "GST_VERIFICATION_ENABLED", False),
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
        self.assertEqual(self.client.post("/api/meta/webhook", json=payload).status_code, 200)

    def _customer(self, balance=0, **tier):
        return _set_tier(self.session, _make_customer(self.session, balance=balance), **tier)

    def _upload(self, message_id="wamid.img.1"):
        self._post(_image(message_id))
        row = self.session.query(WhatsAppIngestion).filter_by(external_message_id=message_id).one()
        self.session.refresh(row)
        return row

    def _row(self, row):
        self.session.expire_all()
        return self.session.get(WhatsAppIngestion, row.id)

    def _used(self):
        self.session.expire_all()
        return self.session.query(Customer).filter_by(whatsapp_id=SENDER).one().trial_credits_used

    def _texts(self):
        return [t or "" for t in self.sent_texts]

    def _assert_choosable(self, row):
        self.assertEqual(row.status, "awaiting_choice")
        self.buttons.assert_awaited_once()
        self.assertFalse(any("recharge" in t.lower() for t in self._texts()), self._texts())

    def _assert_unfunded(self, row, balance):
        self.assertEqual(row.status, "unfunded")
        self.buttons.assert_not_awaited()
        self.assertTrue(any(f"Your wallet balance is ₹{balance}" in t for t in self._texts()), self._texts())


class IntakeTierTests(TierWebhookBase):
    def test_admin_with_zero_balance_gets_buttons(self):
        self._customer(0, tier="ADMIN")
        self._assert_choosable(self._upload())

    def test_bypass_payment_with_zero_balance_gets_buttons(self):
        self._customer(0, tier="STANDARD", bypass=True)
        self._assert_choosable(self._upload())

    def test_trial_with_credits_and_zero_balance_gets_buttons(self):
        self._customer(0, tier="TRIAL", total=2, used=1)
        self._assert_choosable(self._upload())

    def test_trial_exhausted_zero_balance_is_held_like_standard(self):
        self._customer(0, tier="TRIAL", total=2, used=2)
        self._assert_unfunded(self._upload(), 0)

    def test_standard_zero_balance_still_unfunded(self):
        self._customer(0)
        self._assert_unfunded(self._upload(), 0)


class TapTierTests(TierWebhookBase):
    def _tap_and_row(self, button, balance=0, **tier):
        self._customer(balance, **tier)
        row = self._upload()
        self._post(_tap(f"{button}:{row.id}"))
        return self._row(row)

    # ── ADMIN ──
    def test_admin_white_is_free_and_queued(self):
        row = self._tap_and_row("gv_white", 0, tier="ADMIN")
        self.assertEqual((row.product_code, row.amount_charged, row.status), (PRODUCT_WHITE_BG, 0, "white_queued"))
        self.assertEqual(self._balance(), 0)
        self.white_worker.assert_awaited_once_with(row.id)
        self.pack_worker.assert_not_called()

    def test_admin_pack_is_free_and_queued_even_with_funds(self):
        row = self._tap_and_row("gv_pack1", 700, tier="ADMIN")
        self.assertEqual((row.product_code, row.amount_charged, row.status), (PRODUCT_PACK_1, 0, "pack_queued"))
        self.assertEqual(self._balance(), 700)
        self.pack_worker.assert_called_once_with(row.id)

    def test_bypass_payment_white_is_free(self):
        row = self._tap_and_row("gv_white", 0, tier="STANDARD", bypass=True)
        self.assertEqual((row.amount_charged, row.status), (0, "white_queued"))
        self.white_worker.assert_awaited_once_with(row.id)

    # ── TRIAL with credits ──
    def test_trial_white_is_free_queued_and_not_metered_at_tap(self):
        row = self._tap_and_row("gv_white", 0, tier="TRIAL", total=2)
        self.assertEqual((row.amount_charged, row.status), (0, "white_queued"))
        self.assertEqual(self._balance(), 0)
        self.assertEqual(self._used(), 0)
        self.white_worker.assert_awaited_once_with(row.id)

    def test_trial_pack_is_free_queued_and_not_metered_at_tap(self):
        row = self._tap_and_row("gv_pack1", 700, tier="TRIAL", total=1)
        self.assertEqual((row.amount_charged, row.status), (0, "pack_queued"))
        self.assertEqual(self._balance(), 700)
        self.assertEqual(self._used(), 0)
        self.pack_worker.assert_called_once_with(row.id)

    # ── TRIAL shot restriction ──
    def test_trial_restricted_to_white_taps_pack_unfunded_is_refused_and_released(self):
        row = self._tap_and_row("gv_pack1", 0, tier="TRIAL", total=2, shots=["white_bg"])
        self.assertEqual((row.status, row.product_code), ("awaiting_choice", None))
        self.pack_worker.assert_not_called()
        self.white_worker.assert_not_awaited()
        self.assertTrue(any("Clean Studio Shot" in t for t in self._texts()), self._texts())
        self.assertEqual((self._balance(), self._used()), (0, 0))

    def test_trial_restricted_to_pack_taps_white_unfunded_is_refused_and_released(self):
        row = self._tap_and_row("gv_white", 0, tier="TRIAL", total=2, shots=["pack_1"])
        self.assertEqual((row.status, row.product_code), ("awaiting_choice", None))
        self.white_worker.assert_not_awaited()
        self.assertTrue(any("Full Catalog Pack" in t for t in self._texts()), self._texts())

    def test_trial_restricted_then_allowed_product_still_works(self):
        row = self._tap_and_row("gv_pack1", 0, tier="TRIAL", total=2, shots=["white_bg"])
        self._post(_tap(f"gv_white:{row.id}", message_id="wamid.tap.2"))
        row = self._row(row)
        self.assertEqual((row.amount_charged, row.status), (0, "white_queued"))
        self.white_worker.assert_awaited_once_with(row.id)

    def test_trial_restricted_but_wallet_can_pay_is_charged_normally(self):
        row = self._tap_and_row("gv_pack1", 700, tier="TRIAL", total=2, shots=["white_bg"])
        self.assertEqual((row.amount_charged, row.status), (PACK, "pack_queued"))
        self.assertEqual(self._balance(), 700 - PACK)
        self.assertEqual(self._used(), 0)
        self.pack_worker.assert_called_once_with(row.id)

    # ── TRIAL exhausted = STANDARD ──
    def test_trial_exhausted_funded_is_debited(self):
        row = self._tap_and_row("gv_white", 700, tier="TRIAL", total=1, used=1)
        self.assertEqual((row.amount_charged, row.status), (WHITE, "white_queued"))
        self.assertEqual(self._balance(), 650)

    def test_trial_exhausted_underfunded_gets_standard_hold(self):
        row = self._tap_and_row("gv_pack1", 60, tier="TRIAL", total=1, used=1)
        self.assertEqual((row.status, row.product_code), ("awaiting_choice", None))
        self.assertEqual(self._balance(), 60)
        self.pack_worker.assert_not_called()
        self.assertTrue(any("Your wallet balance is ₹60" in t and "₹500 is required for Full Catalog Pack" in t
                            for t in self._texts()), self._texts())

    # ── STANDARD regression ──
    def test_standard_white_charges_50(self):
        row = self._tap_and_row("gv_white", 700)
        self.assertEqual((row.amount_charged, row.status), (WHITE, "white_queued"))
        self.assertEqual(self._balance(), 650)
        self.white_worker.assert_awaited_once_with(row.id)

    def test_standard_pack_charges_500(self):
        row = self._tap_and_row("gv_pack1", 700)
        self.assertEqual((row.amount_charged, row.status), (PACK, "pack_queued"))
        self.assertEqual(self._balance(), 200)
        self.pack_worker.assert_called_once_with(row.id)

    def test_standard_underfunded_pack_is_held(self):
        row = self._tap_and_row("gv_pack1", 499)
        self.assertEqual((row.status, row.product_code), ("awaiting_choice", None))
        self.assertEqual(self._balance(), 499)
        self.pack_worker.assert_not_called()


# ═══ 3. Registration confirmation ══════════════════════════════════════════
REG_FORM = "Name: Anurag Mehta\nbusiness: Moraa Jewels\nGST: NA\nBusiness Address: Surat"


class RegistrationConfirmationTests(TierWebhookBase):
    def _register(self, **tier):
        c = _make_customer(self.session, balance=0)
        c.is_registered = False
        self.session.commit()
        _set_tier(self.session, c, **tier)
        message = {"type": "text", "id": "wamid.reg", "from": SENDER, "timestamp": "1", "text": {"body": REG_FORM}}
        self._post({"object": "whatsapp_business_account", "entry": [{"changes": [
            {"field": "messages", "value": {"messages": [message]}}]}]})

    def _cta_confirmations(self):
        return [c for c in self.cta.await_args_list if "You're all set" in _body(c)]

    def _text_confirmations(self):
        return [t for t in self._texts() if "You're all set" in t]

    def test_admin_gets_plain_text_without_recharge_cta(self):
        self._register(tier="ADMIN")
        self.assertEqual(len(self._text_confirmations()), 1, self._texts())
        self.assertEqual(self._cta_confirmations(), [])

    def test_trial_with_credits_gets_plain_text_without_recharge_cta(self):
        self._register(tier="TRIAL", total=3)
        self.assertEqual(len(self._text_confirmations()), 1, self._texts())
        self.assertEqual(self._cta_confirmations(), [])

    def test_trial_exhausted_gets_recharge_cta(self):
        self._register(tier="TRIAL", total=1, used=1)
        self.assertEqual(len(self._cta_confirmations()), 1)
        self.assertEqual(self._text_confirmations(), [])

    def test_standard_still_gets_recharge_cta(self):
        self._register()
        calls = self._cta_confirmations()
        self.assertEqual(len(calls), 1)
        self.assertTrue(_body(calls[0]).startswith("You're all set, Anurag!"))
        self.assertEqual(self._text_confirmations(), [])


# ═══ 4. Workers: metering, exhaustion alert, cap bypass ════════════════════
class TierWorkerBase(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False},
                                    poolclass=StaticPool)
        Base.metadata.create_all(bind=self.engine)
        self.Session = sessionmaker(bind=self.engine)
        self.tmp = tempfile.TemporaryDirectory()
        photo = Path(self.tmp.name) / "p.jpg"
        self.photo_bytes = b"\xff\xd8\xff" + b"\x01" * 40
        photo.write_bytes(self.photo_bytes)
        db = self.Session()
        image = Image(request_id="req-t", original_filename="p.jpg", stored_filename="p.jpg",
                      file_path=str(photo), file_size=len(self.photo_bytes), mime_type="image/jpeg",
                      image_url="/uploads/req-t/p.jpg")
        db.add(image)
        db.commit()
        self.image_id = image.id
        db.close()

        from app.ai import image_generation_manager as igm
        self.igm = igm
        igm._spend_day, igm._spend_count = None, 0

        self.text = AsyncMock(return_value=True)
        self.cta = AsyncMock(return_value=True)
        self.link = AsyncMock(return_value="https://rzp.io/test")
        self.upload = AsyncMock(side_effect=[f"media-{i}" for i in range(1, 20)])
        self.send_image = AsyncMock(return_value=True)
        self.deliver = AsyncMock(side_effect=lambda **kw: len(kw["image_urls"]))
        self.patches = [
            patch("app.database.SessionLocal", self.Session),
            patch.object(settings, "MAX_GENERATIONS_PER_DAY", 100000),
            patch.object(settings, "GENERATION_ENABLED", True),
            patch.object(settings, "WHATSAPP_PAY_ENABLED", False),
            patch.object(mws, "DRY_RUN_IMAGE_MODE", False),
            patch.object(mws, "send_whatsapp_text", self.text),
            patch.object(mws, "send_whatsapp_cta_url_button", self.cta),
            patch.object(mws, "upload_media_to_meta", self.upload),
            patch.object(mws, "send_image_to_whatsapp", self.send_image),
            patch.object(mws, "send_catalog_pack_images_to_whatsapp", self.deliver),
            patch.object(mws, "_check_failure_rate", lambda db: None),
            patch.object(razorpay_service, "create_recharge_payment_link", self.link),
        ]
        # Names the implementation may have imported by value.
        for module in (mws, es):
            for name, mock in (("create_recharge_payment_link", self.link),
                               ("send_whatsapp_cta_url_button", self.cta),
                               ("send_whatsapp_text", self.text)):
                if module is not mws and hasattr(module, name):
                    self.patches.append(patch.object(module, name, mock))
            if module is mws and hasattr(mws, "create_recharge_payment_link"):
                self.patches.append(patch.object(mws, "create_recharge_payment_link", self.link))
        for p in self.patches:
            p.start()

    def tearDown(self):
        for p in reversed(self.patches):
            p.stop()
        self.igm._spend_day, self.igm._spend_count = None, 0
        self.tmp.cleanup()
        self.engine.dispose()

    def _order(self, product=PRODUCT_WHITE_BG, charged=0, balance=0, **tier):
        db = self.Session()
        c = Customer(whatsapp_id=SENDER, full_name="T", business_name="B", gst_number="N/A",
                     address="N/A", wallet_balance=balance, is_registered=True)
        db.add(c)
        db.commit()
        _set_tier(db, c, **tier)
        row = WhatsAppIngestion(
            external_user_id=SENDER, external_message_id="wamid.w", external_media_id="m",
            channel="whatsapp", image_id=self.image_id, mime_type="image/jpeg", product_code=product,
            status="white_queued" if product == PRODUCT_WHITE_BG else "pack_queued", amount_charged=charged,
        )
        db.add(row)
        db.commit()
        self.oid = row.id
        db.close()

    def _state(self):
        s = self.Session()
        try:
            row = s.get(WhatsAppIngestion, self.oid)
            c = s.query(Customer).filter_by(whatsapp_id=SENDER).one()
            refunds = s.query(AuditLog).filter(AuditLog.action == mws.REFUND_AUDIT_ACTION).count()
            return SimpleNamespace(status=row.status, balance=c.wallet_balance, used=c.trial_credits_used,
                                   refunds=refunds)
        finally:
            s.close()

    def _white(self, gen=None):
        from app.ai.providers.image_base import ImageGenerationResult
        gen = gen or AsyncMock(return_value=ImageGenerationResult(success=True, image_url=DATA_URL,
                                                                  provider_name="gemini"))
        with patch("app.ai.image_generation_manager.ImageGenerationManager.generate_image", gen):
            return asyncio.run(mws.process_whatsapp_white_bg(self.oid))

    def _pack(self, style_result=DATA_URL):
        with patch.object(mws, "_generate_single_pack_style", AsyncMock(return_value=style_result)):
            return asyncio.run(mws.process_whatsapp_catalog_pack(self.oid))

    def _exhaustion_cards(self):
        return [c for c in self.cta.await_args_list if "complimentary test credits" in _body(c)]


class TrialMeteringTests(TierWorkerBase):
    def test_trial_white_success_consumes_one_credit_no_alert_when_credits_left(self):
        self._order(tier="TRIAL", total=3, used=0)
        self.assertTrue(self._white())
        st = self._state()
        self.assertEqual((st.status, st.used, st.balance, st.refunds), ("delivered", 1, 0, 0))
        self.assertEqual(self._exhaustion_cards(), [])

    def test_trial_pack_success_consumes_one_credit(self):
        self._order(product=PRODUCT_PACK_1, tier="TRIAL", total=3, used=1)
        self.assertTrue(self._pack())
        st = self._state()
        self.assertEqual((st.status, st.used, st.balance), ("delivered", 2, 0))
        self.assertEqual(self._exhaustion_cards(), [])

    def test_last_white_credit_sends_exhaustion_recharge_card(self):
        self._order(tier="TRIAL", total=2, used=1)
        self.assertTrue(self._white())
        self.assertEqual(self._state().used, 2)
        cards = self._exhaustion_cards()
        self.assertEqual(len(cards), 1, self.cta.await_args_list)
        self.assertTrue(_body(cards[0]).startswith(EXHAUSTED_PREFIX.format(n=2)), _body(cards[0]))
        call = cards[0]
        url = call.kwargs.get("url") or (call.args[3] if len(call.args) > 3 else None)
        self.assertEqual(url, "https://rzp.io/test")
        self.link.assert_awaited()

    def test_last_pack_credit_sends_exhaustion_recharge_card(self):
        self._order(product=PRODUCT_PACK_1, tier="TRIAL", total=1, used=0)
        self.assertTrue(self._pack())
        self.assertEqual(self._state().used, 1)
        cards = self._exhaustion_cards()
        self.assertEqual(len(cards), 1)
        self.assertTrue(_body(cards[0]).startswith(EXHAUSTED_PREFIX.format(n=1)))

    def test_failed_trial_generation_is_not_counted(self):
        from app.ai.providers.image_base import ImageGenerationResult
        self._order(tier="TRIAL", total=1, used=0)
        gen = AsyncMock(return_value=ImageGenerationResult(success=False, error="boom", provider_name="gemini"))
        self.assertFalse(self._white(gen))
        st = self._state()
        self.assertEqual((st.status, st.used, st.balance), ("failed", 0, 0))
        self.assertEqual(self._exhaustion_cards(), [])

    def test_failed_trial_delivery_is_not_counted(self):
        self._order(tier="TRIAL", total=1, used=0)
        self.send_image.return_value = False
        self.assertFalse(self._white())
        st = self._state()
        self.assertEqual((st.status, st.used), ("delivery_failed", 0))
        self.assertEqual(self._exhaustion_cards(), [])

    def test_failed_trial_pack_is_not_counted(self):
        self._order(product=PRODUCT_PACK_1, tier="TRIAL", total=1, used=0)
        self.assertFalse(self._pack(style_result=None))
        self.assertEqual(self._state().used, 0)
        self.assertEqual(self._exhaustion_cards(), [])

    def test_paid_order_by_trial_customer_does_not_consume_credit(self):
        # Restricted shot bought with wallet money (amount_charged 50).
        self._order(charged=WHITE, balance=650, tier="TRIAL", total=2, used=0, shots=["pack_1"])
        self.assertTrue(self._white())
        st = self._state()
        self.assertEqual((st.status, st.used, st.balance), ("delivered", 0, 650))

    def test_admin_success_never_touches_trial_counter_or_wallet(self):
        self._order(balance=300, tier="ADMIN", total=2, used=0)
        self.assertTrue(self._white())
        st = self._state()
        self.assertEqual((st.status, st.used, st.balance), ("delivered", 0, 300))
        self.assertEqual(self._exhaustion_cards(), [])

    def test_admin_pack_success_never_touches_trial_counter_or_wallet(self):
        self._order(product=PRODUCT_PACK_1, balance=300, tier="ADMIN")
        self.assertTrue(self._pack())
        st = self._state()
        self.assertEqual((st.status, st.used, st.balance), ("delivered", 0, 300))

    def test_standard_success_never_touches_trial_counter(self):
        self._order(charged=WHITE, balance=650)
        self.assertTrue(self._white())
        st = self._state()
        self.assertEqual((st.status, st.used, st.balance, st.refunds), ("delivered", 0, 650, 0))
        self.assertEqual(self._exhaustion_cards(), [])

    def test_record_trial_success_directly(self):
        self._order(tier="TRIAL", total=1, used=0)
        db = self.Session()
        try:
            row = db.get(WhatsAppIngestion, self.oid)
            asyncio.run(es.record_trial_success(db, row))
        finally:
            db.close()
        self.assertEqual(self._state().used, 1)
        self.assertEqual(len(self._exhaustion_cards()), 1)


class AdminDailyCapBypassTests(TierWorkerBase):
    """Real ImageGenerationManager.generate_image, fake provider underneath."""

    def _run_capped(self, kill_switch=False, **order):
        from app.ai.image_generation_manager import ImageGenerationManager
        from app.ai.providers.image_base import ImageGenerationResult

        self._order(**order)
        provider = AsyncMock(return_value=ImageGenerationResult(
            success=True, image_url=DATA_URL, provider_name="gemini"))
        fake = type("P", (), {"is_available": True, "supports_reference_image": lambda self: True,
                               "generate_image": provider})()
        original = ImageGenerationManager.generate_image
        seen = []

        async def spy(mgr, *args, **kwargs):
            seen.append(kwargs)
            return await original(mgr, *args, **kwargs)

        with patch.object(settings, "MAX_GENERATIONS_PER_DAY", 1), \
             patch.object(settings, "GENERATION_ENABLED", True), \
             patch.object(ImageGenerationManager, "_get_provider", lambda self, name: fake), \
             patch.object(ImageGenerationManager, "_get_provider_chain", lambda self: ["gemini"]), \
             patch.object(ImageGenerationManager, "generate_image", spy):
            self.igm._spend_day, self.igm._spend_count = None, 0
            self.assertIsNone(self.igm._spend_blocked())  # today's only slot is now used
            if kill_switch:
                settings.GENERATION_ENABLED = False
            ok = asyncio.run(mws.process_whatsapp_white_bg(self.oid))
        return ok, provider, seen

    def test_admin_order_bypasses_exhausted_daily_cap(self):
        ok, provider, seen = self._run_capped(tier="ADMIN")
        self.assertTrue(ok)
        self.assertEqual(len(seen), 1)
        self.assertTrue(seen[0].get("spend_reserved"), seen)
        provider.assert_awaited_once()
        self.assertEqual(self._state().status, "delivered")

    def test_standard_order_is_still_blocked_and_refunded(self):
        ok, provider, _ = self._run_capped(charged=WHITE, balance=650)
        self.assertFalse(ok)
        provider.assert_not_awaited()
        st = self._state()
        self.assertEqual((st.status, st.balance, st.refunds), ("failed", 700, 1))

    def test_kill_switch_still_blocks_admin(self):
        ok, provider, _ = self._run_capped(kill_switch=True, tier="ADMIN")
        self.assertFalse(ok)
        provider.assert_not_awaited()
        st = self._state()
        self.assertEqual((st.status, st.balance), ("failed", 0))


if __name__ == "__main__":
    unittest.main()
