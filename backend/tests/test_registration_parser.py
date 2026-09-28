"""Plain-text registration parsing: `_parse_registration_text` unit tests plus
webhook-level state checks for the user's `business:` / `Business Address:`
message style. No network call is made; every webhook test gets a fresh DB."""

import unittest

import tests.test_gst_verification as tgv  # module import: avoid re-collecting its test classes
from app.api.routes.meta_webhook import _parse_registration_text
from app.models.customer import Customer
from app.services import gst_service as gst
from tests.test_wallet_funded_slot_gate import SENDER

GOOD = tgv.GOOD
USER_FORM = "Name: MOCK VERIFIED USER\nbusiness: Test Jeweller\nGST: {gst}\nBusiness Address: JOGESHWARI-E TEST"
TEMPLATE = "• Name: Anurag Mehta\n• Brand Name: Moraa Jewels\n• City: Surat\n• GSTIN (Optional): 24AAAPS1234C1Z5"


class ParseRegistrationTextTests(unittest.TestCase):
    def p(self, text):
        return _parse_registration_text(text)

    def test_user_style_invalid_gst(self):
        r = self.p(USER_FORM.format(gst="INVALID1263"))
        self.assertEqual(r["name"], "MOCK")
        self.assertEqual(r["business"], "Test Jeweller")
        self.assertEqual(r["gst"], "N/A")
        self.assertEqual(r["raw_gst"], "INVALID1263")
        self.assertEqual(r["address"], "JOGESHWARI-E TEST")

    def test_official_template_with_bullets(self):
        r = self.p(TEMPLATE)
        self.assertEqual((r["name"], r["business"], r["address"], r["gst"], r["raw_gst"]),
                         ("Anurag", "Moraa Jewels", "Surat", GOOD, GOOD))

    def test_returns_all_keys(self):
        self.assertEqual(set(self.p("Name: A")), {"name", "business", "gst", "raw_gst", "address"})

    def test_key_case_and_spacing_variants(self):
        r = self.p("NAME : Riya Shah\nBUSINESS NAME: Riya Gems\ngstin: 24aaaps1234c1z5 \nCITY: Pune")
        self.assertEqual((r["name"], r["business"], r["gst"], r["address"]), ("Riya", "Riya Gems", GOOD, "Pune"))
        self.assertEqual(r["raw_gst"], "24aaaps1234c1z5")

    def test_brand_key(self):
        self.assertEqual(self.p("Name: A\nBrand: Sparkle")["business"], "Sparkle")

    def test_other_business_keys(self):
        for key in ("Business", "Shop Name", "Company", "Company Name", "Brand Name"):
            self.assertEqual(self.p(f"Name: A\n{key}: Acme")["business"], "Acme", key)

    def test_other_gst_keys(self):
        for key in ("GST", "GSTIN", "GST Number", "GSTIN Number", "GST No", "GST No.", "GSTIN (optional)"):
            r = self.p(f"Name: A\n{key}: {GOOD}")
            self.assertEqual(r["gst"], GOOD, key)

    def test_gst_with_internal_spaces_normalized(self):
        r = self.p("Name: A\nGST: 24aaaps 1234c1z5")
        self.assertEqual(r["gst"], GOOD)
        self.assertEqual(r["raw_gst"], "24aaaps 1234c1z5")

    def test_address_beats_city_regardless_of_order(self):
        self.assertEqual(self.p("Name: A\nAddress: 5 MG Road\nCity: Surat")["address"], "5 MG Road")
        self.assertEqual(self.p("Name: A\nCity: Surat\nAddress: 5 MG Road")["address"], "5 MG Road")
        self.assertEqual(self.p("Name: A\nCity: Surat\nShop Address: 7 Ring Rd")["address"], "7 Ring Rd")

    def test_city_alone_fills_address(self):
        self.assertEqual(self.p("Name: A\nCity: Jaipur")["address"], "Jaipur")

    def test_gst_na(self):
        r = self.p("Name: A\nGST: NA")
        self.assertEqual((r["gst"], r["raw_gst"]), ("N/A", "NA"))

    def test_missing_fields_use_defaults(self):
        r = self.p("Name: Solo")
        self.assertEqual(r, {"name": "Solo", "business": "Jewelry Business", "gst": "N/A",
                             "raw_gst": "", "address": "N/A"})

    def test_no_name_defaults_to_there(self):
        self.assertEqual(self.p("Business: Acme\nCity: Surat")["name"], "there")

    def test_colon_inside_value_kept(self):
        self.assertEqual(self.p("Name: A\nAddress: Shop 5: Main Rd")["address"], "Shop 5: Main Rd")

    def test_business_address_does_not_set_business(self):
        r = self.p("Name: A\nBusiness Address: X")
        self.assertEqual((r["business"], r["address"]), ("Jewelry Business", "X"))

    def test_lines_without_colon_and_empty_values_ignored(self):
        r = self.p("Hello team\nName: Kiran Rao\nBusiness:   \nrandom line\nCity: Surat")
        self.assertEqual((r["name"], r["business"], r["address"]), ("Kiran", "Jewelry Business", "Surat"))

    def test_other_bullet_styles(self):
        r = self.p("* Name: A B\n- Business: Acme\n– City: Surat\n· GST: NA")
        self.assertEqual((r["name"], r["business"], r["address"], r["raw_gst"]), ("A", "Acme", "Surat", "NA"))

    def test_full_name_and_your_name_keys(self):
        self.assertEqual(self.p("Full Name: Meera Jain\nCity: X")["name"], "Meera")
        self.assertEqual(self.p("Your Name: Dev Patel\nCity: X")["name"], "Dev")


class RegistrationWebhookTests(tgv.OnboardingFlowTests):
    """Reuses tests.test_gst_verification.OnboardingFlowTests' harness (fresh DB,
    patched senders, FakeProvider, GST_VERIFICATION_ENABLED=True). Its own test
    methods are masked below so they are not collected twice."""

    def _confirmations(self):
        return self.webhook_module.send_whatsapp_cta_url_button.await_count

    def test_invalid_gst_awaits_gstin_and_keeps_business_name(self):
        self._text(USER_FORM.format(gst="INVALID1263"), "wamid.rp1")
        self.assertEqual(self._confirmations(), 0)
        self.assertTrue(self.button_msgs, "expected invalid-format buttons")
        self.assertTrue(self.button_msgs[-1][0].startswith("Invalid GST format."))
        self.assertEqual([b for b, _ in self.button_msgs[-1][1]], ["btn_gst_reenter", "btn_gst_skip"])
        self.assertEqual(self._state(), gst.STATE_AWAITING_GSTIN)
        c = self._cust()
        self.assertFalse(c.is_registered)
        self.assertEqual(c.business_name, "Test Jeweller")
        self.assertEqual(c.address, "JOGESHWARI-E TEST")
        self.assertEqual(c.full_name.split()[0], "MOCK")
        self.assertEqual(self.provider.calls, [])

    def test_valid_gst_registers_and_confirms_once(self):
        self.provider = tgv.FakeProvider(tgv.active())
        self._text(USER_FORM.format(gst=GOOD), "wamid.rp2")
        c = self._cust()
        self.assertEqual((c.gst_number, c.is_gst_verified, c.is_registered), (GOOD, True, True))
        self.assertEqual(self._state(), gst.STATE_REGISTERED)
        self.assertEqual(self._confirmations(), 1)
        self.assertEqual(self.button_msgs, [])
        self.assertEqual(self.provider.calls, [GOOD])

    def test_gst_na_confirms_immediately_without_lookup(self):
        self._text(USER_FORM.format(gst="NA"), "wamid.rp3")
        self.assertEqual((self._confirmations(), self.button_msgs, self.provider.calls), (1, [], []))
        c = self._cust()
        self.assertEqual((c.gst_number, c.is_registered, c.business_name), ("N/A", True, "Test Jeweller"))

    def test_gst_absent_confirms_immediately_without_lookup(self):
        self._text("Name: MOCK VERIFIED USER\nbusiness: Test Jeweller\nBusiness Address: JOGESHWARI-E TEST", "wamid.rp4")
        self.assertEqual((self._confirmations(), self.button_msgs, self.provider.calls), (1, [], []))
        c = self._cust()
        self.assertEqual((c.gst_number, c.business_name, c.address), ("N/A", "Test Jeweller", "JOGESHWARI-E TEST"))

    def test_name_plus_address_only_is_a_registration(self):
        self._text("Name: Priya Nair\nAddress: 14 Zaveri Bazaar, Mumbai", "wamid.rp5")
        self.session.expire_all()
        c = self.session.query(Customer).filter_by(whatsapp_id=SENDER).first()
        self.assertIsNotNone(c, "Name+Address message was not treated as a registration")
        self.assertEqual(c.address, "14 Zaveri Bazaar, Mumbai")
        self.assertEqual(c.business_name, "Jewelry Business")
        self.assertEqual(self._confirmations(), 1)


# Mask inherited OnboardingFlowTests tests (they already run in their own module).
for _n in dir(tgv.OnboardingFlowTests):
    if _n.startswith("test_") and _n not in RegistrationWebhookTests.__dict__:
        setattr(RegistrationWebhookTests, _n, None)


if __name__ == "__main__":
    unittest.main()
