"""WhatsApp Flow registration: nfm_reply parsing, customer upsert, dedupe,
and the "hi" -> Flow / text-fallback switch. All Meta and Razorpay calls are
mocked; no network call is made."""

import json
import unittest
from unittest.mock import AsyncMock, patch

from app.config import settings
from app.models.audit_log import AuditLog
from app.models.customer import Customer
from app.services import meta_whatsapp_service as mws
from app.services.meta_whatsapp_service import parse_webhook_entry
from tests.test_wallet_funded_slot_gate import SENDER, FundedSlotGateTestCase, _make_customer

GSTIN = "24AAAPS1234C1Z5"
FLOW_ID = "TEST_FLOW_ID"


def _flow_message(response, message_id="wamid.flow1", sender=SENDER):
    return {
        "type": "interactive", "id": message_id, "from": sender, "timestamp": "1700000100",
        "interactive": {"type": "nfm_reply", "nfm_reply": {
            "name": "flow", "body": "Sent",
            "response_json": response if isinstance(response, str) else json.dumps(response),
        }},
    }


def _payload(*messages):
    return {"object": "whatsapp_business_account",
            "entry": [{"changes": [{"field": "messages", "value": {"messages": list(messages)}}]}]}


def _text(body, message_id="wamid.hi", sender=SENDER):
    return {"type": "text", "id": message_id, "from": sender, "timestamp": "1", "text": {"body": body}}


FULL = {"flow_token": "moraa_registration_v1", "full_name": "Anurag Kumar Mehta",
        "business_name": "Moraa Jewels", "address": "12, Diamond Plaza, Surat", "gst_number": ""}


class FlowParsingTests(unittest.TestCase):
    def test_a_nfm_reply_is_parsed(self):
        events = parse_webhook_entry(_payload(_flow_message(FULL))["entry"][0])
        self.assertEqual(len(events), 1)
        ev = events[0]
        self.assertEqual((ev["type"], ev["subtype"], ev["message_id"], ev["sender"], ev["timestamp"]),
                         ("interactive", "nfm_reply", "wamid.flow1", SENDER, "1700000100"))
        self.assertEqual(ev["flow_response"], FULL)

    def test_a_bad_response_json_is_safe(self):
        for raw in ("not json", "[1,2]", ""):
            ev = parse_webhook_entry(_payload(_flow_message(raw))["entry"][0])[0]
            self.assertEqual(ev["subtype"], "nfm_reply")
            self.assertEqual(ev["flow_response"], {})

    def test_g_button_reply_and_other_interactive_unchanged(self):
        btn = {"type": "interactive", "id": "wamid.b", "from": SENDER, "timestamp": "1",
               "interactive": {"type": "button_reply", "button_reply": {"id": "gv_white:x", "title": "t"}}}
        lst = {"type": "interactive", "id": "wamid.l", "from": SENDER, "timestamp": "1",
               "interactive": {"type": "list_reply", "list_reply": {"id": "a"}}}
        ev = parse_webhook_entry(_payload(btn, lst)["entry"][0])
        self.assertEqual(ev[0]["subtype"], "button_reply")
        self.assertEqual(ev[0]["button_reply"], {"id": "gv_white:x", "title": "t"})
        self.assertEqual((ev[1]["type"], ev[1]["raw_type"]), ("unsupported", "interactive:list_reply"))


class FlowSenderTests(unittest.IsolatedAsyncioTestCase):
    async def test_payload_matches_meta_flow_message(self):
        post = AsyncMock(return_value=True)
        with patch.object(settings, "META_REGISTRATION_FLOW_ID", FLOW_ID), \
             patch.object(mws, "_post_message_payload", new=post):
            self.assertTrue(await mws.send_registration_flow(SENDER))
        payload = post.await_args.args[0]
        self.assertEqual(payload["type"], "interactive")
        inter = payload["interactive"]
        self.assertEqual(inter["type"], "flow")
        self.assertEqual(inter["action"]["name"], "flow")
        params = inter["action"]["parameters"]
        self.assertEqual(params["flow_message_version"], "3")
        self.assertEqual(params["flow_id"], FLOW_ID)
        self.assertEqual(params["flow_action"], "navigate")
        self.assertEqual(params["flow_action_payload"], {"screen": "REGISTRATION"})
        self.assertEqual(params["flow_cta"], "Setup Account")
        self.assertEqual(params["flow_token"], f"moraa_reg_{SENDER}")
        self.assertEqual(params["mode"], "draft")

    async def test_published_mode_is_sent_when_configured(self):
        post = AsyncMock(return_value=True)
        with patch.object(settings, "META_REGISTRATION_FLOW_ID", FLOW_ID), \
             patch.object(settings, "META_REGISTRATION_FLOW_MODE", "published"), \
             patch.object(mws, "_post_message_payload", new=post):
            self.assertTrue(await mws.send_registration_flow(SENDER))
        self.assertEqual(post.await_args.args[0]["interactive"]["action"]["parameters"]["mode"], "published")

    async def test_meta_rejection_returns_false(self):
        with patch.object(settings, "META_REGISTRATION_FLOW_ID", FLOW_ID), \
             patch.object(mws, "_post_message_payload", new=AsyncMock(return_value=False)):
            self.assertFalse(await mws.send_registration_flow(SENDER))

    async def test_not_configured_returns_false_without_calling_meta(self):
        post = AsyncMock(return_value=True)
        with patch.object(settings, "META_REGISTRATION_FLOW_ID", ""), \
             patch.object(mws, "_post_message_payload", new=post):
            self.assertFalse(await mws.send_registration_flow(SENDER))
        post.assert_not_awaited()


class FlowWebhookTests(FundedSlotGateTestCase):
    def setUp(self):
        super().setUp()
        self.cta = AsyncMock(return_value=True)
        self.link = AsyncMock(return_value="https://pay")
        self.flow = AsyncMock(return_value=True)
        self.extra = [
            patch.object(self.webhook_module, "send_whatsapp_cta_url_button", new=self.cta),
            patch.object(self.webhook_module, "create_recharge_payment_link", new=self.link),
            patch.object(self.webhook_module, "send_registration_flow", new=self.flow),
        ]
        for p in self.extra:
            p.start()

    def tearDown(self):
        for p in reversed(self.extra):
            p.stop()
        super().tearDown()

    def _post(self, *messages):
        self.assertEqual(self.client.post("/api/meta/webhook", json=_payload(*messages)).status_code, 200)

    def _cust(self):
        self.session.expire_all()
        return self.session.query(Customer).filter_by(whatsapp_id=SENDER).one()

    def test_b_d_e_flow_creates_new_customer_with_full_name_and_na_gst(self):
        self._post(_flow_message(FULL))
        c = self._cust()
        self.assertEqual((c.full_name, c.business_name, c.address, c.gst_number, c.wallet_balance, c.is_registered),
                         ("Anurag Kumar Mehta", "Moraa Jewels", "12, Diamond Plaza, Surat", "N/A", 0, True))
        self.assertEqual(self.cta.await_count, 1)
        self.assertIn("You're all set, Anurag Kumar Mehta!", self.cta.await_args.kwargs["body_text"])
        self.assertEqual(self.link.await_count, 1)

    def test_c_flow_updates_existing_customer_keeps_balance(self):
        _make_customer(self.session, balance=2500)
        self._post(_flow_message(dict(FULL, gst_number=" 24aaaps1234c1z5 ")))
        self.assertEqual(self.session.query(Customer).count(), 1)
        c = self._cust()
        self.assertEqual((c.full_name, c.gst_number, c.wallet_balance), ("Anurag Kumar Mehta", GSTIN, 2500))

    def test_c_flow_adopts_unregistered_payment_placeholder(self):
        self.session.add(Customer(whatsapp_id=SENDER, full_name="Valued Customer", business_name="Jewelry Business",
                                  gst_number="N/A", address="N/A", wallet_balance=1000, is_registered=False))
        self.session.commit()
        self._post(_flow_message(FULL))
        self.assertEqual(self.session.query(Customer).count(), 1)
        c = self._cust()
        self.assertEqual((c.full_name, c.is_registered, c.wallet_balance), ("Anurag Kumar Mehta", True, 1000))

    def test_e_invalid_gst_becomes_na(self):
        self._post(_flow_message(dict(FULL, gst_number="12345")))
        self.assertEqual(self._cust().gst_number, "N/A")

    def test_f_duplicate_message_id_is_ignored(self):
        self._post(_flow_message(FULL, message_id="wamid.dup"))
        # Meta retry of the same submission (even with different content).
        self._post(_flow_message(dict(FULL, full_name="Someone Else"), message_id="wamid.dup"))
        self.assertEqual(self._cust().full_name, "Anurag Kumar Mehta")
        self.assertEqual(self.cta.await_count, 1)
        self.assertEqual(self.link.await_count, 1)
        self.assertEqual(self.session.query(AuditLog).filter_by(action="whatsapp_registration_flow").count(), 1)
        # A NEW submission (new message id) is still processed.
        self._post(_flow_message(dict(FULL, business_name="Moraa Gems"), message_id="wamid.new"))
        self.assertEqual(self._cust().business_name, "Moraa Gems")
        self.assertEqual(self.cta.await_count, 2)

    def test_missing_required_fields_does_not_register(self):
        self._post(_flow_message({"flow_token": "x", "full_name": "  "}))
        self.assertEqual(self.session.query(Customer).count(), 0)
        self.cta.assert_not_awaited()
        self.assertTrue(self.sent_texts and self.sent_texts[-1].startswith("Quick Setup"))

    def test_h_text_registration_still_works(self):
        self._post(_text("• Name: Anurag Mehta\n• Brand Name: Moraa Jewels\n• City: Surat\n• GSTIN (Optional): " + GSTIN,
                         message_id="wamid.txtreg"))
        c = self._cust()
        self.assertEqual((c.full_name, c.business_name, c.address, c.gst_number), ("Anurag", "Moraa Jewels", "Surat", GSTIN))
        self.assertEqual(self.cta.await_count, 1)

    def test_i_hi_sends_flow_for_new_user(self):
        self._post(_text("Hi"))
        self.flow.assert_awaited_once_with(SENDER)
        self.assertEqual(len(self.sent_texts), 1)
        self.assertTrue(self.sent_texts[0].startswith("Welcome to Moraa Studio"))

    def test_j_hi_falls_back_to_text_when_flow_not_sent(self):
        self.flow.return_value = False  # unconfigured Flow ID or Meta rejection
        self._post(_text("Hi"))
        self.assertEqual(len(self.sent_texts), 2)
        self.assertTrue(self.sent_texts[1].startswith("Quick Setup 📋\n\nPlease reply with your details:"))

    def test_j_real_sender_without_flow_id_falls_back(self):
        self.flow.side_effect = mws.send_registration_flow  # real helper, unconfigured
        with patch.object(settings, "META_REGISTRATION_FLOW_ID", ""):
            self._post(_text("Hi"))
        self.assertEqual(len(self.sent_texts), 2)
        self.assertTrue(self.sent_texts[1].startswith("Quick Setup"))

    def test_registered_customer_hi_is_unchanged(self):
        _make_customer(self.session, balance=500)
        self._post(_text("hello"))
        self.flow.assert_not_awaited()
        self.assertEqual(len(self.sent_texts), 2)
        self.assertTrue(self.sent_texts[1].startswith("Quick Setup"))


if __name__ == "__main__":
    unittest.main()
