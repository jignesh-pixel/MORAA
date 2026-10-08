"""Phase 8: the registration Flow asks for an email (docs/whatsapp_registration_flow.json). A valid one is stored
lowercased with the rest of the profile and the customer's Drive folder share is queued; a missing or invalid one never
blocks registration. Emails sent in chat or in the text registration queue the share too. All Meta and Razorpay calls
are mocked; no network call is made."""

import json
import sys
import types
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import app.services as services_package
from app.config import settings
from app.models.customer import Customer
from tests.test_registration_flow import FULL, _flow_message, _payload, _text
from tests.test_wallet_funded_slot_gate import SENDER, FundedSlotGateTestCase, _make_customer

FLOW_JSON = Path(__file__).resolve().parents[2] / "docs" / "whatsapp_registration_flow.json"


class FlowJsonTests(unittest.TestCase):
    def test_email_field_is_on_the_screen_and_in_the_completion_payload(self):
        flow = json.loads(FLOW_JSON.read_text(encoding="utf-8"))
        self.assertEqual(flow["version"], "7.3")
        children = flow["screens"][0]["layout"]["children"]
        email = next(c for c in children if c.get("name") == "email")
        self.assertEqual((email["type"], email["input-type"], email["required"]), ("TextInput", "email", True))
        self.assertLessEqual(len(email["label"]), 20)                       # Meta's TextInput label limit
        self.assertLessEqual(len(email.get("helper-text", "")), 80)
        footer = next(c for c in children if c["type"] == "Footer")
        payload = footer["on-click-action"]["payload"]
        self.assertEqual(payload["email"], "${form.email}")
        self.assertEqual(set(payload), {"full_name", "business_name", "email", "address", "gst_number"})


class RegistrationEmailTests(FundedSlotGateTestCase):
    def setUp(self):
        super().setUp()
        self.cta = AsyncMock(return_value=True)
        self.queue_share = MagicMock(return_value=None)
        patches = [
            patch.object(self.webhook_module, "send_whatsapp_cta_url_button", new=self.cta),
            patch.object(self.webhook_module, "create_recharge_payment_link",
                         new=AsyncMock(return_value="https://pay")),
            patch.object(self.webhook_module, "send_registration_flow", new=AsyncMock(return_value=True)),
            patch.object(settings, "DRIVE_DELIVERY_ENABLED", True),
        ]
        # app.services.drive_layout is written by the Drive work: patch it, or stand in for it until it exists.
        try:
            from app.services import drive_layout
        except ImportError:
            drive_layout = types.ModuleType("app.services.drive_layout")
            patches += [patch.dict(sys.modules, {"app.services.drive_layout": drive_layout}),
                        patch.object(services_package, "drive_layout", drive_layout, create=True)]
        patches.append(patch.object(drive_layout, "queue_share", new=self.queue_share, create=True))
        for p in patches:
            p.start()
            self.addCleanup(p.stop)

    def _post(self, *messages):
        self.assertEqual(self.client.post("/api/meta/webhook", json=_payload(*messages)).status_code, 200)

    def _cust(self):
        self.session.expire_all()
        return self.session.query(Customer).filter_by(whatsapp_id=SENDER).one()

    def test_flow_email_is_stored_lowercased_and_the_share_is_queued(self):
        self._post(_flow_message(dict(FULL, email="  Ravi.Mehta@Gmail.COM ")))
        c = self._cust()
        self.assertEqual((c.full_name, c.is_registered, c.email), ("Anurag Kumar Mehta", True, "ravi.mehta@gmail.com"))
        self.queue_share.assert_called_once_with(c.id)
        self.cta.assert_awaited_once()                       # the usual "You're all set" confirmation

    def test_flow_builder_email_key_is_accepted(self):
        self._post(_flow_message({"screen_0_Full_Name_0": "Anurag", "screen_0_Email_address_2": "a@b.in"}))
        self.assertEqual(self._cust().email, "a@b.in")

    def test_invalid_or_missing_email_still_registers(self):
        self._post(_flow_message(dict(FULL, email="not an email"), message_id="wamid.f1"))
        c = self._cust()
        self.assertEqual((c.is_registered, c.email), (True, None))
        self._post(_flow_message(dict(FULL, business_name="Moraa Gems"), message_id="wamid.f2"))
        c = self._cust()
        self.assertEqual((c.business_name, c.email), ("Moraa Gems", None))
        self.queue_share.assert_not_called()
        self.assertEqual(self.cta.await_count, 2)

    def test_an_invalid_email_keeps_the_stored_one(self):
        cust = _make_customer(self.session, balance=0)
        cust.email = "old@x.in"
        self.session.commit()
        self._post(_flow_message(dict(FULL, email="nope")))
        self.assertEqual(self._cust().email, "old@x.in")

    def test_a_failing_share_never_breaks_registration(self):
        self.queue_share.side_effect = RuntimeError("drive down")
        self._post(_flow_message(dict(FULL, email="a@b.in")))
        self.assertEqual(self._cust().email, "a@b.in")
        self.cta.assert_awaited_once()

    def test_drive_off_queues_nothing(self):
        with patch.object(settings, "DRIVE_DELIVERY_ENABLED", False):
            self._post(_flow_message(dict(FULL, email="a@b.in")))
        self.assertEqual(self._cust().email, "a@b.in")
        self.queue_share.assert_not_called()

    def test_chat_and_text_registration_emails_queue_the_share(self):
        self._post(_text("Name: Ravi\nBrand: X\nCity: Y\nEmail: Ravi@X.com", message_id="wamid.t1"))
        self._post(_text("new@y.in", message_id="wamid.t2"))
        c = self._cust()
        self.assertEqual(c.email, "new@y.in")
        self.assertEqual([call.args for call in self.queue_share.call_args_list], [(c.id,), (c.id,)])


if __name__ == "__main__":
    unittest.main()
