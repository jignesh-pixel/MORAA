"""₹50 Ecommerce Shot prompt + 'no balance change on hi / registration'."""

import unittest

from app.models.customer import Customer
from app.services import wallet_service
from app.services.ecommerce_shot_prompt import build_ecommerce_shot_prompt
from tests.test_wallet_funded_slot_gate import SENDER, FundedSlotGateTestCase, _make_customer


def _text(body, message_id="wamid.txt", sender=SENDER):
    return {"object": "whatsapp_business_account", "entry": [{"changes": [{"field": "messages", "value": {
        "messages": [{"type": "text", "id": message_id, "from": sender, "timestamp": "1", "text": {"body": body}}]}}]}]}


REGISTRATION = "Name: Anurag\nBusiness name: Moraa\nGST number: NA\nBusiness address: Surat"


class EcommerceShotPromptTests(unittest.TestCase):
    def test_prompt_is_a_catalogue_shot_of_the_reference_earring(self):
        p = build_ecommerce_shot_prompt()
        low = p.lower()
        for must in ("attached customer photo", "study the reference", "isolate", "#ffffff",
                     "studio softbox lighting", "soft, natural, light-grey drop shadow",
                     "amazon / myntra", "reference image priority: maximum",
                     "blank or empty white image", "no model, no ear, no hand"):
            self.assertIn(must, low, must)

    def test_pack_1_prompt_is_not_used_for_the_50_product(self):
        from app.services.earring_ecommerce_prompt import build_earring_ecommerce_prompt
        self.assertNotEqual(build_ecommerce_shot_prompt(), build_earring_ecommerce_prompt())


class GreetingAndRegistrationNeverTouchBalanceTests(FundedSlotGateTestCase):
    def _post(self, payload):
        self.assertEqual(self.client.post("/api/meta/webhook", json=payload).status_code, 200)

    def test_hi_and_registration_keep_existing_balance(self):
        _make_customer(self.session, balance=2500)
        for i, body in enumerate(("Hi", "hii", "hello", REGISTRATION, "start", REGISTRATION)):
            self._post(_text(body, message_id=f"wamid.t{i}"))
            self.assertEqual(self._balance(), 2500, body)
        self.assertEqual(self.session.query(Customer).count(), 1)

    def test_new_customer_registration_starts_at_zero(self):
        self._post(_text("Hi"))
        self.assertIsNone(self.session.query(Customer).filter_by(whatsapp_id=SENDER).first())
        self._post(_text(REGISTRATION, message_id="wamid.reg"))
        cust = self.session.query(Customer).filter_by(whatsapp_id=SENDER).one()
        self.assertEqual(cust.wallet_balance, 0)
        self._post(_text(REGISTRATION, message_id="wamid.reg2"))
        self.assertEqual(wallet_service.get_balance(self.session, SENDER), 0)


NEW_FORM = "• Name: Anurag Mehta\n• Brand Name: Moraa Jewels\n• City: Surat\n• GSTIN (Optional):"


class NewRegistrationTemplateTests(FundedSlotGateTestCase):
    def _post(self, payload):
        self.assertEqual(self.client.post("/api/meta/webhook", json=payload).status_code, 200)

    def test_hi_sends_the_new_registration_request(self):
        self._post(_text("Hi"))
        self.assertEqual(self.sent_texts, [
            "Welcome to Moraa Studio ✨\n\n"
            "We transform your raw jewelry photos into studio-grade product visuals in seconds.\n\n"
            "Let’s quickly set up your account!",
            "Quick Setup 📋\n\nPlease reply with your details:\n\n"
            "• Name:\n• Brand Name:\n• City:\n• GSTIN (Optional):",
        ])

    def test_new_form_with_bullets_and_blank_gstin_registers_at_zero(self):
        from unittest.mock import AsyncMock, patch

        cta = AsyncMock(return_value=True)
        with patch.object(self.webhook_module, "send_whatsapp_cta_url_button", new=cta), \
             patch.object(self.webhook_module, "create_recharge_payment_link", new=AsyncMock(return_value="https://pay")):
            self._post(_text(NEW_FORM, message_id="wamid.new"))
        cust = self.session.query(Customer).filter_by(whatsapp_id=SENDER).one()
        self.assertEqual((cust.full_name, cust.business_name, cust.gst_number, cust.address, cust.wallet_balance),
                         ("Anurag", "Moraa Jewels", "N/A", "Surat", 0))
        kwargs = cta.await_args.kwargs
        self.assertEqual(kwargs["body_text"],
                         "You're all set, Anurag! 🎉\n\nYour account is ready.\n"
                         "Wallet Balance: ₹0\n\nRecharge your wallet below to get started:")
        self.assertEqual(kwargs["button_label"], "Recharge Wallet")

    def test_confirmation_shows_existing_balance(self):
        from unittest.mock import AsyncMock, patch

        _make_customer(self.session, balance=2500)
        cta = AsyncMock(return_value=True)
        with patch.object(self.webhook_module, "send_whatsapp_cta_url_button", new=cta), \
             patch.object(self.webhook_module, "create_recharge_payment_link", new=AsyncMock(return_value="https://pay")):
            self._post(_text(NEW_FORM, message_id="wamid.new2"))
        self.assertIn("Wallet Balance: ₹2,500", cta.await_args.kwargs["body_text"])
        self.assertEqual(self._balance(), 2500)


if __name__ == "__main__":
    unittest.main()
