"""Live GSTIN verification in onboarding: format pre-check, provider parsing,
verified / rejected / timeout outcomes, Re-enter / Skip buttons, and the
flag-off path staying exactly as before. No network call is made."""

import asyncio
import json
import unittest
from unittest.mock import AsyncMock, patch

from app.config import settings
from app.models.customer import Customer
from app.models.onboarding_session import OnboardingSession
from app.services import gst_service as gst
from app.services import meta_whatsapp_service as mws
from tests.test_wallet_funded_slot_gate import SENDER, FundedSlotGateTestCase, _make_customer

GOOD = "24AAAPS1234C1Z5"


class FakeProvider(gst.GstProvider):
    name = "fake"

    def __init__(self, payload=None, exc=None, delay=0.0):
        self.payload, self.exc, self.delay, self.calls = payload, exc, delay, []

    async def lookup(self, gstin):
        self.calls.append(gstin)
        if self.delay:
            await asyncio.sleep(self.delay)
        if self.exc:
            raise self.exc
        return self.payload


def active(gstin=GOOD, trade="SHAH GEMS LLP"):
    return {"data": {"gstin": gstin, "sts": "Active", "tradeNam": trade, "lgnm": "SHAH GEMS LIMITED LIABILITY",
                     "pradr": {"addr": {"bno": "12", "st": "Diamond Plaza", "loc": "Surat", "stcd": "Gujarat", "pncd": "395003"}}}}


class FormatAndParsingTests(unittest.TestCase):
    def test_format_precheck(self):
        self.assertEqual(gst.normalize_gstin(" 24aaaps 1234c1z5 "), GOOD)
        self.assertTrue(gst.is_valid_gstin_format(" 24aaaps1234c1z5"))
        for bad in ("", "12345", "24AAAPS1234C0Z5", "24AAAPS1234C1X5", "24AAAPS1234C1Z5X"):
            self.assertFalse(gst.is_valid_gstin_format(bad), bad)

    def test_invalid_format_never_calls_provider(self):
        p = FakeProvider(active())
        r = asyncio.run(gst.verify_gstin("27ABCDE1234", provider=p))
        self.assertEqual(r.status, gst.INVALID_FORMAT)
        self.assertEqual(p.calls, [])

    def test_active_payload_parsed_from_nested_data(self):
        r = asyncio.run(gst.verify_gstin(GOOD, provider=FakeProvider({"result": active()})))
        self.assertTrue(r.verified)
        self.assertEqual((r.trade_name, r.display_name), ("SHAH GEMS LLP", "SHAH GEMS LLP"))
        self.assertEqual(r.address, "12, Diamond Plaza, Surat, Gujarat, 395003")

    def test_inactive_not_found_and_mismatch(self):
        cancelled = {"data": {"gstin": GOOD, "sts": "Cancelled", "lgnm": "X"}}
        self.assertEqual(asyncio.run(gst.verify_gstin(GOOD, provider=FakeProvider(cancelled))).status, gst.INACTIVE)
        self.assertEqual(asyncio.run(gst.verify_gstin(GOOD, provider=FakeProvider({"error": "no record"}))).status, gst.NOT_FOUND)
        self.assertEqual(asyncio.run(gst.verify_gstin(GOOD, provider=FakeProvider(active(gstin="27AAAPS1234C1Z5")))).status, gst.NOT_FOUND)
        self.assertEqual(asyncio.run(gst.verify_gstin(GOOD, provider=FakeProvider(exc=gst.GstLookupNotFound("404")))).status, gst.NOT_FOUND)

    def test_timeout_and_errors_are_unavailable(self):
        with patch.object(settings, "GST_API_TIMEOUT_SECONDS", 0.05):
            r = asyncio.run(gst.verify_gstin(GOOD, provider=FakeProvider(active(), delay=1)))
        self.assertEqual((r.status, r.detail), (gst.UNAVAILABLE, "timeout"))
        r = asyncio.run(gst.verify_gstin(GOOD, provider=FakeProvider(exc=RuntimeError("HTTP 500"))))
        self.assertEqual(r.status, gst.UNAVAILABLE)

    def test_default_provider_is_none_and_mock_needs_debug(self):
        self.assertEqual(type(settings).model_fields["GST_PROVIDER"].default, "none")
        self.assertFalse(type(settings).model_fields["GST_VERIFICATION_ENABLED"].default)
        self.assertEqual(asyncio.run(gst.verify_gstin(GOOD, provider=gst.NoGstProvider())).status, gst.UNAVAILABLE)
        with patch.object(settings, "GST_PROVIDER", "mock"), patch.object(settings, "DEBUG", False):
            self.assertIsInstance(gst.get_gst_provider(), gst.NoGstProvider)
        with patch.object(settings, "GST_PROVIDER", "mock"), patch.object(settings, "DEBUG", True):
            self.assertIsInstance(gst.get_gst_provider(), gst.MockGstProvider)


REG_FORM = "• Name: Anurag Mehta\n• Brand Name: Moraa Jewels\n• City: Surat\n• GSTIN (Optional): {gst}"


class OnboardingFlowTests(FundedSlotGateTestCase):
    def setUp(self):
        super().setUp()
        self.texts, self.button_msgs = [], []
        self.provider = FakeProvider(active())

        async def text(recipient_id, message_text=None, **_):
            self.texts.append(message_text)
            return True

        async def buttons(recipient_id, body_text, buttons, **_):
            self.button_msgs.append((body_text, buttons))
            return True

        self.extra = [
            patch.object(mws, "send_whatsapp_text", new=AsyncMock(side_effect=text)),
            patch.object(mws, "send_reply_buttons", new=AsyncMock(side_effect=buttons)),
            patch.object(self.webhook_module, "send_whatsapp_cta_url_button", new=AsyncMock(return_value=True)),
            patch.object(self.webhook_module, "create_recharge_payment_link", new=AsyncMock(return_value="https://pay")),
            patch.object(gst, "get_gst_provider", side_effect=lambda: self.provider),
            patch.object(settings, "GST_VERIFICATION_ENABLED", True),
        ]
        for p in self.extra:
            p.start()

    def tearDown(self):
        for p in reversed(self.extra):
            p.stop()
        super().tearDown()

    def _post(self, message):
        payload = {"object": "whatsapp_business_account",
                   "entry": [{"changes": [{"field": "messages", "value": {"messages": [message]}}]}]}
        self.assertEqual(self.client.post("/api/meta/webhook", json=payload).status_code, 200)

    def _text(self, body, mid):
        self._post({"type": "text", "id": mid, "from": SENDER, "timestamp": "1", "text": {"body": body}})

    def _button(self, bid, mid):
        self._post({"type": "interactive", "id": mid, "from": SENDER, "timestamp": "1",
                    "interactive": {"type": "button_reply", "button_reply": {"id": bid, "title": "x"}}})

    def _flow(self, response, mid="wamid.flow"):
        self._post({"type": "interactive", "id": mid, "from": SENDER, "timestamp": "1",
                    "interactive": {"type": "nfm_reply", "nfm_reply": {"name": "flow", "body": "Sent",
                                                                       "response_json": json.dumps(response)}}})

    def _cust(self):
        self.session.expire_all()
        return self.session.query(Customer).filter_by(whatsapp_id=SENDER).one()

    def _state(self):
        self.session.expire_all()
        row = self.session.query(OnboardingSession).filter_by(whatsapp_id=SENDER).first()
        return row.state if row else None

    def test_verified_gstin_saves_registry_details_and_completes_profile(self):
        _make_customer(self.session, balance=700)
        self._text(REG_FORM.format(gst=GOOD.lower()), "wamid.r1")
        c = self._cust()
        self.assertEqual((c.gst_number, c.is_gst_verified, c.business_name, c.address),
                         (GOOD, True, "SHAH GEMS LLP", "12, Diamond Plaza, Surat, Gujarat, 395003"))
        self.assertIn("✅ GSTIN Verified Successfully! Trade Name: SHAH GEMS LLP. Profile complete.", self.texts)
        self.assertEqual(self.button_msgs, [])
        self.assertEqual(self._state(), gst.STATE_REGISTERED)
        self.assertEqual(self._balance(), 700)  # wallet untouched

    def test_rejected_gstin_from_flow_offers_reenter_or_skip(self):
        self.provider = FakeProvider({"data": {"gstin": GOOD, "sts": "Cancelled"}})
        self._flow({"full_name": "Anurag Mehta", "business_name": "Moraa Jewels", "address": "Surat", "gst_number": GOOD})
        c = self._cust()
        self.assertEqual((c.gst_number, c.is_gst_verified, c.business_name), ("N/A", False, "Moraa Jewels"))
        body, buttons = self.button_msgs[-1]
        self.assertIn("Cancelled", body)
        self.assertEqual(buttons, [("btn_gst_reenter", "Re-enter GSTIN"), ("btn_gst_skip", "Skip for now")])

    def test_invalid_format_skips_api_and_offers_choice(self):
        self._flow({"full_name": "A B", "business_name": "Moraa", "address": "Surat", "gst_number": "27ABC123"})
        self.assertEqual(self.provider.calls, [])
        self.assertTrue(self.button_msgs[-1][0].startswith("Invalid GST format. A valid GSTIN must be 15 alphanumeric characters (e.g., 27ABCDE1234F1Z5)."))
        self.assertFalse(self._cust().is_gst_verified)

    def test_api_timeout_offers_retry_and_keeps_entered_gstin(self):
        self.provider = FakeProvider(exc=asyncio.TimeoutError())
        self._flow({"full_name": "A B", "business_name": "Moraa", "address": "Surat", "gst_number": GOOD})
        c = self._cust()
        self.assertEqual((c.gst_number, c.is_gst_verified), (GOOD, False))
        self.assertIn("isn't responding", self.button_msgs[-1][0])

    def test_reenter_then_valid_gstin_verifies(self):
        self.provider = FakeProvider({"data": {"gstin": GOOD, "sts": "Suspended"}})
        self._flow({"full_name": "A B", "business_name": "Moraa", "address": "Surat", "gst_number": GOOD})
        self._button("btn_gst_reenter", "wamid.b1")
        self.assertEqual(self._state(), gst.STATE_AWAITING_GSTIN)
        self.assertEqual(self.texts[-1], "Please enter your 15-digit GSTIN number:")
        self.provider = FakeProvider(active(gstin="27AAAPS1234C1Z5", trade="MORAA NEW"))
        self._text(" 27aaaps1234c1z5 ", "wamid.t2")
        c = self._cust()
        self.assertEqual((c.gst_number, c.is_gst_verified, c.business_name), ("27AAAPS1234C1Z5", True, "MORAA NEW"))
        self.assertEqual(self._state(), gst.STATE_REGISTERED)
        # Back to normal routing: a greeting is no longer read as a GSTIN.
        calls = len(self.provider.calls)
        self._text("Hi", "wamid.t3")
        self.assertEqual(len(self.provider.calls), calls)

    def test_skip_clears_gstin_and_continues(self):
        _make_customer(self.session, balance=500)
        self._button("btn_gst_skip", "wamid.b2")
        c = self._cust()
        self.assertEqual((c.gst_number, c.is_gst_verified), ("N/A", False))
        self.assertEqual(self.texts[-1], gst.SKIP_MESSAGE)
        self.assertTrue(gst.SKIP_MESSAGE.startswith("Understood! You can proceed without GST verification."))
        self.assertEqual(self._state(), gst.STATE_REGISTERED)
        self.assertEqual(self._balance(), 500)

    def test_typing_skip_while_awaiting_gstin(self):
        _make_customer(self.session, balance=0)
        self._button("btn_gst_reenter", "wamid.b3")
        self._text("skip", "wamid.t4")
        self.assertEqual(self.texts[-1], gst.SKIP_MESSAGE)
        self.assertEqual(self.provider.calls, [])

    def test_no_gstin_given_means_no_verification(self):
        self._flow({"full_name": "A B", "business_name": "Moraa", "address": "Surat", "gst_number": ""})
        self.assertEqual((self.provider.calls, self.button_msgs), ([], []))

    def test_flag_off_keeps_old_behaviour(self):
        with patch.object(settings, "GST_VERIFICATION_ENABLED", False):
            self._text(REG_FORM.format(gst=GOOD), "wamid.off1")
            self._button("btn_gst_skip", "wamid.off2")
        c = self._cust()
        self.assertEqual((c.gst_number, c.is_gst_verified, c.business_name), (GOOD, False, "Moraa Jewels"))
        self.assertEqual((self.provider.calls, self.button_msgs), ([], []))
        self.assertIsNone(self._state())


if __name__ == "__main__":
    unittest.main()
