"""F18: an email address sent on WhatsApp is saved on the customer and never read as a greeting or a recharge
command ("pay500@gmail.com", "hi@brandjewels.in"). All Meta and Razorpay calls are mocked; no network call is made."""

import asyncio
import unittest
from contextlib import contextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from loguru import logger

from app.api.routes import meta_webhook
from app.api.routes.meta_webhook import _extract_email
from app.models.customer import Customer
from app.services import data_lifecycle as dl
from tests.test_registration_flow import _payload, _text
from tests.test_wallet_funded_slot_gate import SENDER, FundedSlotGateTestCase, _make_customer
from tests.test_wallet_ledger import LedgerTestBase


@contextmanager
def _captured_logs():
    messages = []
    sink_id = logger.add(lambda m: messages.append(m.record["message"]), level="DEBUG")
    try:
        yield messages
    finally:
        logger.remove(sink_id)


class ExtractEmailTests(unittest.TestCase):
    def test_a_valid_addresses_are_found_and_lowercased(self):
        cases = {
            "ravi.hey@outlook.com": "ravi.hey@outlook.com",
            "pay500@gmail.com": "pay500@gmail.com",
            "Ravi.Hey@Outlook.COM": "ravi.hey@outlook.com",
            "my email is ravi@x.com.": "ravi@x.com",
            "(ravi@x.com)": "ravi@x.com",
            "user+tag@mail.example.co.in, thanks": "user+tag@mail.example.co.in",
            "a" * 64 + "@x.com": "a" * 64 + "@x.com",
            "old: a@x.com new: b@y.com": "a@x.com",
            "*ravi@x.com*": "ravi@x.com",                       # WhatsApp bold
            "_ravi_k@x.com_": "ravi_k@x.com",                   # WhatsApp italic; the inner underscore stays
        }
        for text, expected in cases.items():
            self.assertEqual(_extract_email(text), expected, text)

    def test_a_invalid_addresses_are_ignored(self):
        too_long = "a" * 64 + "@" + ("x" * 63 + ".") * 3 + "com"           # 260 chars > 254
        for text in ("no at sign", "a@b", "a..b@x.com", ".a@x.com", "a.@x.com", "a@-x.com", "a@x.c0m",
                     "a" * 65 + "@x.com", too_long, "", None):
            self.assertIsNone(_extract_email(text), text)


class EmailWebhookTests(FundedSlotGateTestCase):
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

    def _post(self, body, message_id):
        self.assertEqual(self.client.post("/api/meta/webhook", json=_payload(_text(body, message_id))).status_code, 200)

    def _cust(self):
        self.session.expire_all()
        return self.session.query(Customer).filter_by(whatsapp_id=SENDER).one()

    def test_b_pay_prefixed_email_is_saved_not_a_recharge(self):
        _make_customer(self.session, balance=0)
        with _captured_logs() as logs:
            self._post("pay500@gmail.com", "wamid.e1")
        self.assertEqual(self._cust().email, "pay500@gmail.com")
        self.link.assert_not_awaited()
        self.cta.assert_not_awaited()
        self.assertEqual(len(self.sent_texts), 1)
        self.assertIn("pay500@gmail.com", self.sent_texts[0])
        self.assertFalse(any("pay500@gmail.com" in m for m in logs))

    def test_c_greeting_like_emails_are_saved_without_the_welcome(self):
        _make_customer(self.session, balance=500)
        cases = (("hi@brandjewels.in", "hi@brandjewels.in"),
                 ("shop.start@gmail.com", "shop.start@gmail.com"),
                 ("my email is Ravi.Hey@outlook.com", "ravi.hey@outlook.com"))
        for i, (text, expected) in enumerate(cases):
            self._post(text, f"wamid.c{i}")
            self.assertEqual(self._cust().email, expected, text)
        self.flow.assert_not_awaited()
        self.assertEqual(len(self.sent_texts), 3)
        self.assertFalse(any(t.startswith(("Welcome", "Quick Setup")) for t in self.sent_texts))

    def test_d_unknown_sender_is_asked_to_register_and_no_row_is_created(self):
        self._post("ravi.hey@outlook.com", "wamid.d1")
        self.assertEqual(self.session.query(Customer).count(), 0)
        self.assertEqual(len(self.sent_texts), 1)
        self.assertIn("send HI", self.sent_texts[0])
        self.flow.assert_not_awaited()

    def test_e_recharge_and_greeting_still_work(self):
        _make_customer(self.session, balance=0)
        self._post("recharge 500", "wamid.r1")
        self.link.assert_awaited_once()
        self.assertEqual(self.link.await_args.kwargs["amount"], 500)
        self.cta.assert_awaited_once()
        self._post("hi", "wamid.h1")
        self.assertEqual(len(self.sent_texts), 1)                    # registered: a welcome back, never the form
        self.assertTrue(self.sent_texts[0].startswith("Welcome back, Ananya!"))
        self.assertNotIn("Quick Setup", self.sent_texts[0])
        self.assertIsNone(self._cust().email)

    def test_f_registration_text_with_email_registers_and_saves_it(self):
        self._post("Name: Ravi\nBrand: X\nCity: Y\nEmail: Ravi@X.com", "wamid.f1")
        c = self._cust()
        self.assertEqual((c.full_name, c.business_name, c.address, c.is_registered, c.email),
                         ("Ravi", "X", "Y", True, "ravi@x.com"))
        self.cta.assert_awaited_once()                     # the usual "You're all set" confirmation


class SaveEmailFailureTests(unittest.TestCase):
    def test_a_failed_commit_rolls_back_and_asks_to_retry_without_logging_the_address(self):
        db = MagicMock()
        db.commit.side_effect = RuntimeError("UPDATE customers SET email='ravi@x.com'")
        texts = AsyncMock(return_value=True)
        with patch.object(meta_webhook, "_find_customer_safe", return_value=SimpleNamespace(email=None)), \
             patch.object(meta_webhook, "send_whatsapp_text", new=texts), _captured_logs() as logs:
            asyncio.run(meta_webhook._save_customer_email(db, SENDER, "ravi@x.com"))
        db.rollback.assert_called_once()
        self.assertIn("couldn't save", texts.await_args.args[1])
        self.assertTrue(logs)
        self.assertFalse(any("ravi@x.com" in m for m in logs))


class ErasureClearsEmailTests(LedgerTestBase):
    def test_g_erasure_clears_the_email(self):
        customer = self.make_customer(0)
        customer.email = "ravi@x.com"
        self.db.commit()
        dl.erase_customer(self.db, customer)
        self.db.expire_all()
        self.assertIsNone(self.db.get(Customer, customer.id).email)


if __name__ == "__main__":
    unittest.main()
