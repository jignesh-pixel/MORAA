"""Phase 2 / MON-5: phone numbers are compared in international format; a foreign number that merely
ends in the same 10 digits must never reach an Indian customer's wallet."""

import unittest

from app.api.routes.meta_webhook import _same_sender
from app.models.customer import Customer
from app.services import wallet_service
from app.services.whatsapp_pay_service import _allowlisted
from app.utils.phone import normalize_phone, same_phone
from tests.test_wallet_ledger import LedgerTestBase

INDIAN = "919876543210"
UK_LOOKALIKE = "449876543210"          # different country, same last 10 digits


class NormalizePhoneTests(unittest.TestCase):
    def test_every_common_spelling_of_an_indian_number_is_the_same(self):
        for raw in ("919876543210", "+919876543210", "+91 98765-43210", "9876543210", "09876543210",
                    "0091 9876543210", "(+91) 98765 43210", " 91-9876543210 ", "+91 0 98765 43210"):
            self.assertEqual(normalize_phone(raw), INDIAN, raw)

    def test_foreign_numbers_keep_their_country_code(self):
        self.assertEqual(normalize_phone("+44 9876543210"), UK_LOOKALIKE)
        self.assertEqual(normalize_phone("14155550123"), "14155550123")

    def test_empty_and_garbage(self):
        self.assertEqual(normalize_phone(None), "")
        self.assertEqual(normalize_phone(""), "")
        self.assertEqual(normalize_phone("+-() "), "")

    def test_ids_with_letters_are_never_turned_into_a_number(self):
        self.assertEqual(normalize_phone("cust_Q123456789012"), "cust_Q123456789012")
        self.assertFalse(same_phone("cust_Q123456789012", "123456789012"))

    def test_same_phone(self):
        self.assertTrue(same_phone("+919876543210", "9876543210"))
        self.assertFalse(same_phone(INDIAN, UK_LOOKALIKE))
        self.assertFalse(same_phone("", ""))


class LookupTests(LedgerTestBase):
    def test_foreign_lookalike_does_not_find_the_indian_wallet(self):
        self.make_customer(500, whatsapp_id=INDIAN)
        self.assertIsNone(wallet_service.find_customer_by_phone(self.db, UK_LOOKALIKE))
        self.assertIsNone(wallet_service.find_customer_by_phone(self.db, "+" + UK_LOOKALIKE))

    def test_indian_spellings_find_the_wallet(self):
        customer = self.make_customer(500, whatsapp_id=INDIAN)
        for raw in (INDIAN, "+919876543210", "9876543210", "+91 98765 43210"):
            found = wallet_service.find_customer_by_phone(self.db, raw)
            self.assertIsNotNone(found, raw)
            self.assertEqual(found.id, customer.id)

    def test_old_rows_stored_without_a_country_code_are_still_found_by_indian_numbers_only(self):
        legacy = self.make_customer(500, whatsapp_id="9876543210")
        self.assertEqual(wallet_service.find_customer_by_phone(self.db, INDIAN).id, legacy.id)
        self.assertIsNone(wallet_service.find_customer_by_phone(self.db, UK_LOOKALIKE))

    def test_two_countries_with_the_same_last_digits_are_two_wallets(self):
        indian = self.make_customer(500, whatsapp_id=INDIAN)
        uk = self.make_customer(300, whatsapp_id=UK_LOOKALIKE)
        self.assertEqual(wallet_service.find_customer_by_phone(self.db, INDIAN).id, indian.id)
        self.assertEqual(wallet_service.find_customer_by_phone(self.db, UK_LOOKALIKE).id, uk.id)
        self.assertEqual(self.db.query(Customer).count(), 2)


class SenderChecksTests(unittest.TestCase):
    def test_same_sender(self):
        self.assertTrue(_same_sender(INDIAN, "+919876543210"))
        self.assertFalse(_same_sender(INDIAN, UK_LOOKALIKE))

    def test_whatsapp_pay_allowlist_does_not_admit_a_lookalike(self):
        from unittest.mock import patch

        from app.config import settings

        with patch.object(settings, "WHATSAPP_PAY_ALLOWLIST", "9876543210, +91 91234 56789"):
            self.assertTrue(_allowlisted(INDIAN))
            self.assertTrue(_allowlisted("919123456789"))
            self.assertFalse(_allowlisted(UK_LOOKALIKE))


if __name__ == "__main__":
    unittest.main()
