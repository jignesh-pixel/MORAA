"""Catalog Pack v1.5: the third product (5 styles), independent of Studio Shot (WHITE_BG) and Ecomm Pack 1 (PACK_1).

Covers its definition (5 styles, its own list), identifiers (product code, ledger SKU, button, collection row), pricing
and cart parsing (sku_pack_v1_5_N), the menus (behind CATALOG_V1_5_ENABLED), the credit ledger (own SKU, own expiry
reference, refund, claw-back), choosing the product (credits only, no wallet, no trial), the worker (5 calls, spend
reservation, partial delivery n/5, Drive naming, refund on failure), payments (Razorpay link + webhook, WhatsApp Pay
order lines + grant, invoice lines) and the routing of retries / the outbox. All Meta, Razorpay and AI calls are
mocked; no network call is made and no credits are used. The two older products' tests are untouched.
"""

import asyncio
import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from fastapi import BackgroundTasks
from sqlalchemy.orm import sessionmaker

import app.models  # noqa: F401 -- register every model with Base.metadata
from app import config
from app.api.routes import meta_webhook, payment_routes
from app.config import settings
from app.database import Base, get_db
from app.models.audit_log import AuditLog
from app.models.customer import Customer
from app.models.image import Image
from app.models.razorpay_payment_link import RazorpayPaymentLink
from app.models.sku_credit import (
    ACTION_CONSUME,
    ACTION_EXPIRE,
    ACTION_PURCHASE,
    ACTION_REFUND,
    SKU_CREATIVE,
    SKU_CREATIVE_V1_5,
    SKU_WHITE_BG,
    CustomerSkuCredit,
)
from app.models.whatsapp_ingestion import (
    CREDIT_SOURCE_SKU,
    PRODUCT_PACK_1,
    PRODUCT_PACK_V1_5,
    PRODUCT_WHITE_BG,
    WhatsAppIngestion,
)
from app.models.whatsapp_payment_order import WhatsAppPaymentOrder
from app.services import billing_service, dashboard_service, outbox, pricing, razorpay_service, sku_messages, sku_packs
from app.services import meta_whatsapp_service as mws
from app.services import whatsapp_pay_service
from app.services.earring_close_up_ears_prompt import build_close_up_ears_prompt
from app.services.earring_ecommerce_prompt import build_earring_ecommerce_prompt
from app.services.earring_on_stand_shot import build_stand_shot_prompt
from app.services.earring_professional_shot_prompt import build_professional_shot_prompt
from app.services.earring_scale_reference_prompt import build_scale_reference_prompt
from tests.db_support import make_engine
from tests.test_pack_menu import PackWebhookBase, _list_reply, _order, _session_factory
from tests.test_wallet_funded_slot_gate import _make_customer
from tests.test_wallet_ledger import SENDER, LedgerTestBase, assert_ledger_matches_balances

EXPECTED_STYLES = [
    ("Clean E-Commerce", "prompt_ecommerce"),
    ("Close-up on Ear", "prompt_close_up"),
    ("Scale Reference", "prompt_scale_reference"),
    ("Professional Studio", "prompt_professional"),
    ("Stand Display", "prompt_stand"),
]
EXPECTED_PROMPTS = [build_earring_ecommerce_prompt(), build_close_up_ears_prompt(), build_scale_reference_prompt(),
                    build_professional_shot_prompt(), build_stand_shot_prompt()]
LONG_AGO = datetime.now(timezone.utc) - timedelta(days=400)


# ── 1. definition and identifiers ───────────────────────────────────────────────────────────────────────────

class DefinitionTests(unittest.TestCase):
    def test_exactly_five_styles_in_this_order(self):
        self.assertEqual(mws.CATALOG_V1_5_PACK_STYLES, EXPECTED_STYLES)
        self.assertEqual(len(mws.CATALOG_V1_5_PACK_STYLES), 5)
        self.assertEqual(mws.pack_v1_5_generation_count(), 5)

    def test_it_is_its_own_list_and_leaves_pack_1_alone(self):
        self.assertIsNot(mws.CATALOG_V1_5_PACK_STYLES, mws.CATALOG_PACK_STYLES)
        self.assertEqual(len(mws.CATALOG_PACK_STYLES), 8)
        self.assertEqual(mws.pack_generation_count(), 8)
        self.assertEqual(config._PACK_IMAGE_COUNT, 8)
        self.assertNotIn(("Lifestyle Shot", "prompt_complementary"), mws.CATALOG_V1_5_PACK_STYLES)
        self.assertIn("generating all 8 styles", mws.CATALOG_PACK_ACK_TEMPLATE)

    def test_the_ack_names_the_pack_and_its_style_count(self):
        self.assertIn("Catalog Pack v1.5", mws.CATALOG_V1_5_PACK_ACK_TEMPLATE)
        self.assertIn("generating all 5 styles", mws.CATALOG_V1_5_PACK_ACK_TEMPLATE)

    def test_three_products_have_three_distinct_identifiers(self):
        self.assertEqual((PRODUCT_WHITE_BG, PRODUCT_PACK_1, PRODUCT_PACK_V1_5), ("WHITE_BG", "PACK_1", "PACK_V1_5"))
        self.assertEqual((SKU_WHITE_BG, SKU_CREATIVE, SKU_CREATIVE_V1_5), ("white_bg", "creative_pack",
                                                                           "creative_pack_v1_5"))
        self.assertEqual(mws.PRODUCT_BUTTON_PACK_V1_5, "gv_pack_v1_5")
        self.assertEqual(mws.COLLECTION_CATALOG_V1_5, "gv_col_catalog_v1_5")
        self.assertEqual(len({mws.PRODUCT_BUTTON_WHITE, mws.PRODUCT_BUTTON_PACK_1, mws.PRODUCT_BUTTON_PACK_V1_5}), 3)
        self.assertEqual(len(set(mws.COLLECTION_IDS)), 3)

    def test_product_button_ids_are_parsed_for_all_three_products(self):
        for button in (mws.PRODUCT_BUTTON_WHITE, mws.PRODUCT_BUTTON_PACK_1, mws.PRODUCT_BUTTON_PACK_V1_5):
            self.assertEqual(mws.parse_product_button_id(mws.product_button_id(button, "ing-1")), (button, "ing-1"))
        for bad in ("gv_pack_v1:ing", "gv_pack_v1_5", "gv_pack_v1_5:", "", None):
            self.assertIsNone(mws.parse_product_button_id(bad))

    def test_order_labels(self):
        self.assertEqual(mws.order_label("WHITE_BG"), "Clean Studio Shot")
        self.assertEqual(mws.order_label("PACK_V1_5"), "Catalog Pack v1.5")
        self.assertEqual(mws.order_label("PACK_1"), "Full Catalog Pack")
        self.assertEqual(mws.order_label(None), "Full Catalog Pack")             # a legacy row is Pack 1
        self.assertEqual(meta_webhook.PRODUCT_LABELS[PRODUCT_PACK_V1_5], "Catalog Pack v1.5")


# ── 2. pricing and cart parsing ─────────────────────────────────────────────────────────────────────────────

class PricingTests(unittest.TestCase):
    def test_default_price_is_350_a_sku(self):
        self.assertEqual(settings.CATALOG_V1_5_PACK_SKU_PRICE, 350)
        self.assertEqual(pricing.catalog_v1_5_pack_price(), 350)

    def test_tier_ids_are_read_as_v1_5_credits(self):
        for size in (1, 5, 20, 50, 100):
            self.assertEqual(pricing.catalog_v1_5_retailer_id(size), f"sku_pack_v1_5_{size}")
            self.assertEqual(pricing.parse_retailer_id(f"sku_pack_v1_5_{size}"), ("creative_pack_v1_5", size))
            self.assertEqual(pricing.parse_retailer_id(f"SKU_PACK_V1_5_{size}"), ("creative_pack_v1_5", size))
            # the two older products read exactly as before
            self.assertEqual(pricing.parse_retailer_id(f"sku_pack_{size}"), ("creative_pack", size))
            self.assertEqual(pricing.parse_retailer_id(f"studio_sku_{size}"), ("white_bg", size))
        for bad in ("sku_pack_v1_5_3", "sku_pack_v1_5_", "sku_pack_v1_5_x", "sku_pack_v1_", "sku_pack_v1_5"):
            self.assertIsNone(pricing.parse_retailer_id(bad))

    def test_a_v1_5_cart_is_priced_on_the_server(self):
        quote = pricing.quote_cart([{"retailer_id": "sku_pack_v1_5_20", "quantity": 1, "item_price": 1}])
        self.assertEqual((quote["white_units"], quote["creative_packs"], quote["v1_5_packs"]), (0, 0, 20))
        self.assertEqual((quote["v1_5_total"], quote["total"]), (7000, 7000))

    def test_a_mixed_cart_keeps_each_product_apart(self):
        quote = pricing.quote_cart([{"retailer_id": "studio_sku_5", "quantity": 1},
                                    {"retailer_id": "sku_pack_5", "quantity": 1},
                                    {"retailer_id": "sku_pack_v1_5_5", "quantity": 2}])
        self.assertEqual((quote["white_units"], quote["creative_packs"], quote["v1_5_packs"]), (5, 5, 10))
        self.assertEqual((quote["white_total"], quote["creative_total"], quote["v1_5_total"]), (100, 2500, 3500))
        self.assertEqual(quote["total"], 6100)

    def test_carts_without_v1_5_are_unchanged(self):
        quote = pricing.quote_cart([{"retailer_id": "sku_pack_20", "quantity": 1}])
        self.assertEqual((quote["v1_5_packs"], quote["v1_5_total"], quote["total"]), (0, 0, 10000))

    def test_the_price_is_a_setting_and_the_cart_is_capped(self):
        with patch.object(settings, "CATALOG_V1_5_PACK_SKU_PRICE", 400):
            self.assertEqual(pricing.quote_cart([{"retailer_id": "sku_pack_v1_5_5", "quantity": 1}])["total"], 2000)
        quote = pricing.quote_cart([{"retailer_id": "sku_pack_v1_5_100", "quantity": 10 ** 9}])
        self.assertEqual(quote["v1_5_packs"], pricing.MAX_CART_UNITS)

    def test_catalogue_sync_for_the_older_collections_is_unchanged_and_v1_5_has_its_own(self):
        older = [r["retailer_id"] for r in pricing.catalogue_requests(20)]
        self.assertEqual(len(older), 10)
        self.assertFalse(any("v1_5" in rid for rid in older))
        requests = pricing.catalogue_requests_v1_5()
        self.assertEqual([r["retailer_id"] for r in requests], [f"sku_pack_v1_5_{n}" for n in (1, 5, 20, 50, 100)])
        self.assertEqual(requests[2]["data"]["price"], 20 * 350 * pricing.PAISE_PER_RUPEE)
        self.assertEqual(requests[2]["data"]["name"], "Catalog Pack v1.5 20 SKUs")

    def test_price_summary_lists_the_v1_5_price(self):
        summary = pricing.price_summary()
        self.assertEqual(summary["catalog_v1_5_pack_sku_price"], 350)
        self.assertEqual(summary["catalog_v1_5_packs"][20], 7000)


class CustomerCopyTests(unittest.TestCase):
    def test_cart_text_lists_v1_5(self):
        body = sku_messages.pack_link_body(0, 1750, 0, 5)
        self.assertIn("Catalog Pack v1.5: 5 SKUs", body)
        self.assertIn("Total SKUs: 5", body)
        self.assertIn("₹1,750", body)
        mixed = sku_messages.pack_link_body(5, 4350, 5, 5)
        for line in ("Studio Shot: 5 SKUs", "Catalog Pack: 5 SKUs", "Catalog Pack v1.5: 5 SKUs", "Total SKUs: 15"):
            self.assertIn(line, mixed)
        self.assertTrue(sku_messages.pack_order_body(0, 1750, 0, 5).endswith("Tap Review and pay to pay here in WhatsApp."))

    def test_cart_text_without_v1_5_is_exactly_as_before(self):
        self.assertEqual(sku_messages.pack_link_body(20, 400),
                         "Your Cart is ready!\nTotal SKUs: 20\nTotal Amount: ₹400 (incl. GST)\n"
                         "Click below to complete your payment:")
        self.assertNotIn("v1.5", sku_messages.pack_link_body(5, 2600, 5))

    def test_rows_fit_whatsapp_limits(self):
        self.assertEqual(sku_messages.catalog_v1_5_collection_row(), "Pack v1.5 · 5 styles · ₹350 per SKU")
        self.assertEqual(sku_messages.catalog_v1_5_tier_description(20), "20 SKUs of 5 photoshoot styles · ₹7,000")
        self.assertLessEqual(len(sku_messages.catalog_v1_5_tier_description(100)), 72)
        self.assertLessEqual(len(sku_messages.CATALOG_V1_5_COLLECTION_TITLE), 24)


# ── 3. menus (behind CATALOG_V1_5_ENABLED) ─────────────────────────────────────────────────────────────────

class MenuTests(LedgerTestBase):
    def collection_rows(self):
        with patch("app.database.SessionLocal", sessionmaker(bind=self.engine)):
            return mws._collection_rows()

    def test_off_by_default_the_collections_are_the_two_older_ones(self):
        self.assertFalse(settings.CATALOG_V1_5_ENABLED)
        self.assertEqual([r[0] for r in self.collection_rows()], [mws.COLLECTION_STUDIO, mws.COLLECTION_CATALOG])
        self.assertNotIn("3.", sku_messages.collections_body())
        self.assertNotIn("v1.5", sku_messages.collections_body())

    def test_enabled_adds_a_third_collection_row_and_line(self):
        with patch.object(settings, "CATALOG_V1_5_ENABLED", True):
            rows = self.collection_rows()
            body = sku_messages.collections_body()
        self.assertEqual([r[0] for r in rows], [mws.COLLECTION_STUDIO, mws.COLLECTION_CATALOG,
                                                mws.COLLECTION_CATALOG_V1_5])
        self.assertEqual(rows[2][1:], ("Catalog Pack v1.5", "Pack v1.5 · 5 styles · ₹350 per SKU"))
        self.assertLessEqual(len(rows[2][1]), 24)
        self.assertLessEqual(len(rows[2][2]), 72)
        self.assertIn("3. Catalog Pack v1.5: 5 jewellery photoshoot styles per photo, ₹350 per SKU\n\n"
                      "Tap View Collections", body)
        self.assertIn("(Ecomm Pack 1): 8 jewellery photoshoot styles", body)

    def test_the_v1_5_collection_lists_only_its_own_tier_items(self):
        self.assertEqual(mws._collection_retailer_ids(mws.COLLECTION_CATALOG_V1_5),
                         [f"sku_pack_v1_5_{n}" for n in pricing.menu_pack_sizes()])
        self.assertTrue(all(i.startswith("sku_pack_") and "v1_5" not in i
                            for i in mws._collection_retailer_ids(mws.COLLECTION_CATALOG)))

    def test_picking_the_v1_5_collection_sends_a_product_list_titled_for_it(self):
        with patch.object(settings, "META_CATALOG_ID", "CAT1"), \
                patch.object(mws, "_post_message_payload", new=AsyncMock(return_value=True)) as post:
            asyncio.run(mws.send_collection_products(SENDER, mws.COLLECTION_CATALOG_V1_5))
        interactive = post.await_args.args[0]["interactive"]
        self.assertEqual(interactive["header"]["text"], "Catalog Pack v1.5")
        (section,) = interactive["action"]["sections"]
        ids = [i["product_retailer_id"] for i in section["product_items"]]
        self.assertTrue(ids and all(i.startswith("sku_pack_v1_5_") for i in ids))

    def test_the_legacy_radio_menu_is_unchanged(self):
        with patch("app.database.SessionLocal", sessionmaker(bind=self.engine)):
            ids = [r[0] for r in mws._pack_menu_rows()]
        self.assertFalse(any("v1_5" in i for i in ids))

    def test_the_choice_buttons_show_v1_5_only_to_a_holder_of_its_credits(self):
        post = AsyncMock(return_value=True)
        with patch.object(mws, "_post_message_payload", new=post):
            asyncio.run(mws.send_product_selection_buttons(SENDER, "ing-1", 50, 500, 100))
            two = post.await_args.args[0]["interactive"]
            asyncio.run(mws.send_product_selection_buttons(SENDER, "ing-1", 50, 500, 100, v1_5_credits=3))
            three = post.await_args.args[0]["interactive"]
        self.assertEqual([b["reply"]["id"] for b in two["action"]["buttons"]], ["gv_white:ing-1", "gv_pack1:ing-1"])
        self.assertNotIn("v1.5", two["body"]["text"])
        self.assertEqual([b["reply"]["id"] for b in three["action"]["buttons"]],
                         ["gv_white:ing-1", "gv_pack1:ing-1", "gv_pack_v1_5:ing-1"])
        self.assertEqual(three["action"]["buttons"][2]["reply"]["title"], "Pack v1.5 (3 left)")
        self.assertTrue(all(len(b["reply"]["title"]) <= 20 for b in three["action"]["buttons"]))
        self.assertIn("Catalog Pack v1.5 (3 SKUs left)", three["body"]["text"])
        self.assertTrue(three["body"]["text"].endswith("Wallet Balance: ₹100"))


class CartRoutingTests(PackWebhookBase):
    """A catalogue cart or list pick with v1.5 tiers becomes one payment link carrying the v1.5 count."""

    def test_a_v1_5_cart_is_one_link_for_v1_5_only(self):
        _make_customer(self.session, balance=0)
        self._post(_order([("sku_pack_v1_5_20", 1)]))
        self.pack_link.assert_awaited_once()
        self.assertEqual(self.pack_link.await_args.kwargs, {
            "customer_phone": self.pack_link.await_args.kwargs["customer_phone"], "customer_name": "Ananya Shah",
            "units": 0, "creative_packs": 0, "v1_5_packs": 20})
        body = self.cta.await_args.kwargs["body_text"]
        self.assertIn("Catalog Pack v1.5: 20 SKUs", body)
        self.assertIn("Total Amount: ₹7,000 (incl. GST)", body)

    def test_a_mixed_cart_is_one_link_for_every_product(self):
        _make_customer(self.session, balance=0)
        self._post(_order([("studio_sku_5", 1), ("sku_pack_5", 1), ("sku_pack_v1_5_5", 1)]))
        kwargs = self.pack_link.await_args.kwargs
        self.assertEqual((kwargs["units"], kwargs["creative_packs"], kwargs["v1_5_packs"]), (5, 5, 5))
        self.assertIn("Total Amount: ₹4,350 (incl. GST)", self.cta.await_args.kwargs["body_text"])

    def test_a_list_pick_of_a_v1_5_tier_is_one_link(self):
        _make_customer(self.session, balance=0)
        self._post(_list_reply("sku_pack_v1_5_5"))
        kwargs = self.pack_link.await_args.kwargs
        self.assertEqual((kwargs["units"], kwargs["creative_packs"], kwargs["v1_5_packs"]), (0, 0, 5))
        self.assertEqual(self.cta.await_args.kwargs["reply_to_message_id"], "wamid.list1")

    def test_carts_without_v1_5_pass_no_v1_5_argument_at_all(self):
        _make_customer(self.session, balance=0)
        self._post(_order([("sku_pack_5", 1)]))
        self.assertEqual(self.pack_link.await_args.kwargs, {
            "customer_phone": self.pack_link.await_args.kwargs["customer_phone"], "customer_name": "Ananya Shah",
            "units": 0, "creative_packs": 5})

    def test_a_cart_of_only_unknown_ids_is_still_unreadable(self):
        _make_customer(self.session, balance=0)
        self._post(_order([("sku_pack_v1_5_7", 1), ("sku_pack_v1_6_5", 1)]))
        self.pack_link.assert_not_awaited()
        self.menu.assert_awaited()


# ── 4. the credit ledger ────────────────────────────────────────────────────────────────────────────────────

class LedgerTests(LedgerTestBase):
    concurrent = True

    def balance(self, customer, sku):
        self.db.expire_all()
        return sku_packs.balance(self.db, customer.id, sku)

    def grant(self, customer, units, sku, payment_id="pay_v15", now=None):
        sku_packs.grant_pack_credits(self.db, customer, units, payment_id, now=now, sku=sku)
        self.db.commit()

    def test_v1_5_credits_are_their_own_balance(self):
        customer = self.make_customer(300)
        self.grant(customer, 5, SKU_CREATIVE_V1_5)
        self.assertEqual([self.balance(customer, s) for s in (SKU_WHITE_BG, SKU_CREATIVE, SKU_CREATIVE_V1_5)], [0, 0, 5])
        row = self.db.query(CustomerSkuCredit).filter_by(action=ACTION_PURCHASE).one()
        self.assertEqual((row.sku, row.quantity, row.reference_id), ("creative_pack_v1_5", 5,
                                                                    "pay_v15:creative_pack_v1_5"))
        self.assertEqual(self.db.get(Customer, customer.id).wallet_balance, 300)       # credits never touch the wallet
        assert_ledger_matches_balances(self, self.db)

    def test_consuming_one_product_never_uses_another(self):
        customer = self.make_customer(0)
        self.grant(customer, 2, SKU_WHITE_BG, "pay_a")
        self.grant(customer, 2, SKU_CREATIVE, "pay_a")
        self.grant(customer, 2, SKU_CREATIVE_V1_5, "pay_a")
        self.assertTrue(sku_packs.consume_credit(self.db, customer, "ing-1", SKU_CREATIVE_V1_5))
        self.db.commit()
        self.assertEqual([self.balance(customer, s) for s in (SKU_WHITE_BG, SKU_CREATIVE, SKU_CREATIVE_V1_5)], [2, 2, 1])
        only_other = self.make_customer(0, whatsapp_id="919111111111")
        self.grant(only_other, 3, SKU_CREATIVE, "pay_b")
        self.assertFalse(sku_packs.consume_credit(self.db, only_other, "ing-2", SKU_CREATIVE_V1_5))

    def test_a_failed_order_gives_the_credit_back_to_v1_5(self):
        customer = self.make_customer(0)
        self.grant(customer, 3, SKU_CREATIVE_V1_5)
        self.assertTrue(sku_packs.consume_credit(self.db, customer, "ing-1", SKU_CREATIVE_V1_5))
        self.db.commit()
        self.assertTrue(sku_packs.refund_credit(self.db, "ing-1"))
        self.assertFalse(sku_packs.refund_credit(self.db, "ing-1"))                   # once
        self.assertEqual(self.balance(customer, SKU_CREATIVE_V1_5), 3)
        self.assertEqual(self.db.query(CustomerSkuCredit).filter_by(action=ACTION_REFUND).one().sku,
                         "creative_pack_v1_5")

    def test_catalog_and_v1_5_credits_expiring_the_same_day_both_expire(self):
        customer = self.make_customer(0)
        self.grant(customer, 2, SKU_CREATIVE, "pay_a", now=LONG_AGO)
        self.grant(customer, 3, SKU_CREATIVE_V1_5, "pay_a", now=LONG_AGO)
        now = datetime.now(timezone.utc)
        self.assertTrue(sku_packs._expire_customer(self.db, customer.id, now, SKU_CREATIVE))
        self.assertTrue(sku_packs._expire_customer(self.db, customer.id, now, SKU_CREATIVE_V1_5))
        self.assertEqual([self.balance(customer, s) for s in (SKU_CREATIVE, SKU_CREATIVE_V1_5)], [0, 0])
        references = {r.sku: r.reference_id for r in self.db.query(CustomerSkuCredit).filter_by(action=ACTION_EXPIRE)}
        self.assertEqual(len(set(references.values())), 2)
        self.assertTrue(references["creative_pack_v1_5"].endswith(":v15"))
        self.assertTrue(references["creative_pack"].endswith(":c"))                  # the older reference is unchanged

    def test_a_refunded_cart_takes_back_every_product_in_it(self):
        customer = self.make_customer(0)
        self.grant(customer, 20, SKU_WHITE_BG, "pay_cart")
        self.grant(customer, 5, SKU_CREATIVE_V1_5, "pay_cart")
        paid = 20 * pricing.sku_price() + 5 * pricing.catalog_v1_5_pack_price()
        result = sku_packs.claw_back_pack(self.db, payment_id="pay_cart", entity_id="rfnd_1", amount_rupees=paid,
                                          kind="refund")
        self.assertEqual((result["outcome"], result["taken"], result["shortfall"]), ("applied", 25, 0))
        self.assertEqual([self.balance(customer, s) for s in (SKU_WHITE_BG, SKU_CREATIVE_V1_5)], [0, 0])

    def test_a_v1_5_purchase_alone_can_be_clawed_back_and_a_repeat_is_ignored(self):
        customer = self.make_customer(0)
        self.grant(customer, 5, SKU_CREATIVE_V1_5, "pay_v15only")
        first = sku_packs.claw_back_pack(self.db, payment_id="pay_v15only", entity_id="rfnd_2", amount_rupees=1750,
                                         kind="refund")
        again = sku_packs.claw_back_pack(self.db, payment_id="pay_v15only", entity_id="rfnd_2", amount_rupees=1750,
                                         kind="refund")
        self.assertEqual((first["outcome"], first["taken"]), ("applied", 5))
        self.assertEqual(again["outcome"], "duplicate")
        self.assertEqual(self.balance(customer, SKU_CREATIVE_V1_5), 0)

    def test_dashboard_shapes_are_unchanged_until_someone_holds_v1_5(self):
        customer = self.make_customer(0)
        self.grant(customer, 2, SKU_CREATIVE, "pay_a")
        self.assertEqual(dashboard_service.sku_stats(self.db)["credits_outstanding"], {"white_bg": 0, "creative_pack": 2})
        self.grant(customer, 4, SKU_CREATIVE_V1_5, "pay_a")
        self.assertEqual(dashboard_service.sku_stats(self.db)["credits_outstanding"],
                         {"white_bg": 0, "creative_pack": 2, "creative_pack_v1_5": 4})


# ── 5. choosing the product ─────────────────────────────────────────────────────────────────────────────────

class ProductChoiceTests(LedgerTestBase):
    """Tapping "Pack v1.5" on a stored photo: paid by its own SKU credit only (no wallet, no trial)."""

    def setUp(self):
        super().setUp()
        self.sent = AsyncMock()
        for p in (patch.object(meta_webhook, "send_whatsapp_text", new=self.sent),
                  patch.object(meta_webhook, "try_send_native_recharge", new=AsyncMock(return_value=True)),
                  patch.object(mws, "DRY_RUN_IMAGE_MODE", False)):
            p.start()
            self.addCleanup(p.stop)
        from app.ai import image_generation_manager as igm
        self._saved = (igm._spend_day, igm._spend_count)
        igm._spend_day, igm._spend_count = None, 0
        self.addCleanup(lambda: setattr(igm, "_spend_day", self._saved[0]))
        self.addCleanup(lambda: setattr(igm, "_spend_count", self._saved[1]))

    def choose(self, ingestion, button=mws.PRODUCT_BUTTON_PACK_V1_5):
        return asyncio.run(meta_webhook._handle_product_choice(self.db, SENDER, button, ingestion.id))

    def credits(self, customer, sku=SKU_CREATIVE_V1_5):
        self.db.expire_all()
        return sku_packs.balance(self.db, customer.id, sku)

    def give(self, customer, units, sku=SKU_CREATIVE_V1_5):
        sku_packs.grant_pack_credits(self.db, customer, units, "pay_x", sku=sku)
        self.db.commit()

    def texts(self):
        return [call.args[1] for call in self.sent.await_args_list]

    def test_a_credit_pays_and_the_v1_5_worker_is_queued(self):
        customer = self.make_customer(1000)
        self.give(customer, 3)
        ingestion = self.make_ingestion()
        job = self.choose(ingestion)
        self.assertEqual(job, (meta_webhook.process_whatsapp_catalog_v1_5, ingestion.id))
        self.db.expire_all()
        row = self.db.get(WhatsAppIngestion, ingestion.id)
        self.assertEqual((row.status, row.product_code, row.credit_source, row.amount_charged),
                         ("pack_queued", "PACK_V1_5", CREDIT_SOURCE_SKU, None))
        self.assertEqual(self.credits(customer), 2)
        self.assertEqual(self.db.get(Customer, customer.id).wallet_balance, 1000)      # the wallet is not touched
        self.assertTrue(any("Catalog Pack v1.5" in t and "all 5 styles" in t for t in self.texts()))
        assert_ledger_matches_balances(self, self.db)

    def test_without_credits_it_is_declined_and_the_wallet_is_never_charged(self):
        customer = self.make_customer(1000)
        ingestion = self.make_ingestion()
        self.assertIsNone(self.choose(ingestion))
        self.db.expire_all()
        row = self.db.get(WhatsAppIngestion, ingestion.id)
        self.assertEqual((row.status, row.product_code, row.amount_charged), ("awaiting_choice", None, None))
        self.assertEqual(self.db.get(Customer, customer.id).wallet_balance, 1000)
        self.assertEqual(self.texts()[-1], meta_webhook.V1_5_NO_CREDIT_MESSAGE)
        self.assertIn("View Collections", self.texts()[-1])
        assert_ledger_matches_balances(self, self.db)

    def test_other_products_credits_cannot_pay_for_it(self):
        customer = self.make_customer(0)
        self.give(customer, 5, SKU_WHITE_BG)
        self.give(customer, 5, SKU_CREATIVE)
        ingestion = self.make_ingestion()
        self.assertIsNone(self.choose(ingestion))
        self.assertEqual([self.credits(customer, s) for s in (SKU_WHITE_BG, SKU_CREATIVE)], [5, 5])

    def test_pack_1_does_not_use_v1_5_credits(self):
        customer = self.make_customer(1000)
        self.give(customer, 5)
        ingestion = self.make_ingestion()
        job = self.choose(ingestion, button=mws.PRODUCT_BUTTON_PACK_1)
        self.assertEqual(job, (meta_webhook.process_whatsapp_catalog_pack, ingestion.id))      # paid from the wallet instead
        self.assertEqual(self.credits(customer), 5)
        self.db.expire_all()
        self.assertEqual(self.db.get(Customer, customer.id).wallet_balance, 500)
        self.assertEqual(self.db.get(WhatsAppIngestion, ingestion.id).product_code, "PACK_1")

    def test_the_studio_shot_still_runs_its_own_worker(self):
        customer = self.make_customer(1000)
        ingestion = self.make_ingestion()
        job = self.choose(ingestion, button=mws.PRODUCT_BUTTON_WHITE)
        self.assertEqual(job, (meta_webhook.process_whatsapp_white_bg, ingestion.id))

    def test_a_whole_pack_must_fit_the_daily_cap_before_a_credit_is_used(self):
        customer = self.make_customer(0)
        self.give(customer, 3)
        ingestion = self.make_ingestion()
        with patch.object(settings, "MAX_GENERATIONS_PER_DAY", 4):                       # 5 styles need 5 slots
            self.assertIsNone(self.choose(ingestion))
        self.assertEqual(self.credits(customer), 3)
        self.assertIn("nothing was charged", self.texts()[-1])
        with patch.object(settings, "MAX_GENERATIONS_PER_DAY", 5):
            self.assertIsNotNone(self.choose(ingestion))
        self.assertEqual(self.credits(customer), 2)

    def test_a_double_tap_uses_one_credit(self):
        customer = self.make_customer(0)
        self.give(customer, 3)
        ingestion = self.make_ingestion()
        self.assertIsNotNone(self.choose(ingestion))
        self.assertIsNone(self.choose(ingestion))
        self.assertEqual(self.credits(customer), 2)

    def test_a_trial_customer_cannot_use_trial_credits_for_it(self):
        customer = self.make_customer(0)
        customer.tier, customer.trial_credits_total, customer.trial_credits_used = "TRIAL", 5, 0
        self.db.commit()
        ingestion = self.make_ingestion()
        self.assertIsNone(self.choose(ingestion))
        self.db.expire_all()
        self.assertEqual(self.db.get(Customer, customer.id).trial_credits_used, 0)
        self.assertEqual(self.texts()[-1], meta_webhook.V1_5_NO_CREDIT_MESSAGE)

    def test_team_access_is_free(self):
        customer = self.make_customer(0)
        customer.tier = "ADMIN"
        self.db.commit()
        ingestion = self.make_ingestion()
        self.assertEqual(self.choose(ingestion), (meta_webhook.process_whatsapp_catalog_v1_5, ingestion.id))
        self.db.expire_all()
        self.assertEqual(self.db.get(WhatsAppIngestion, ingestion.id).amount_charged, 0)

    def test_the_failure_notice_names_the_product(self):
        self.assertEqual(mws.order_label(PRODUCT_PACK_V1_5), "Catalog Pack v1.5")


# ── 6. the worker ───────────────────────────────────────────────────────────────────────────────────────────

class WorkerBase(unittest.TestCase):
    """A Catalog Pack v1.5 order exactly as _handle_product_choice leaves it: paid by one v1.5 SKU credit."""

    def setUp(self):
        self.engine = make_engine(concurrent=True)
        Base.metadata.create_all(bind=self.engine)
        self.Session = sessionmaker(bind=self.engine)
        db = self.Session()
        customer = Customer(whatsapp_id=SENDER, full_name="T", business_name="B", gst_number="N/A", address="N/A",
                            wallet_balance=200, is_registered=True)
        db.add(customer)
        self.tmp = tempfile.TemporaryDirectory()
        photo = Path(self.tmp.name) / "p.jpg"
        self.photo_bytes = b"\xff\xd8\xff" + b"\x01" * 40
        photo.write_bytes(self.photo_bytes)
        image = Image(request_id="req-v15", original_filename="p.jpg", stored_filename="p.jpg", file_path=str(photo),
                      file_size=len(self.photo_bytes), mime_type="image/jpeg", image_url="/uploads/req-v15/p.jpg")
        db.add(image)
        db.commit()
        row = WhatsAppIngestion(external_user_id=SENDER, external_message_id="wamid.v15", external_media_id="m",
                                channel="whatsapp", status="pack_queued", product_code=PRODUCT_PACK_V1_5,
                                credit_source=CREDIT_SOURCE_SKU, amount_charged=None, image_id=image.id,
                                mime_type="image/jpeg")
        db.add(row)
        db.commit()
        self.oid, self.customer_id = row.id, customer.id
        sku_packs.grant_pack_credits(db, customer, 3, "pay_v15", sku=SKU_CREATIVE_V1_5)
        sku_packs.consume_credit(db, customer, self.oid, SKU_CREATIVE_V1_5)            # the tap used one: 2 left
        db.commit()
        db.close()
        self.text = AsyncMock(return_value=True)
        self.upload = AsyncMock(side_effect=[f"media-{i}" for i in range(1, 6)])
        self.deliver = AsyncMock(side_effect=lambda **kw: len(kw["image_urls"]))
        from app.ai import image_generation_manager as igm
        igm._spend_day, igm._spend_count = None, 0
        self.patches = [patch("app.database.SessionLocal", self.Session),
                        patch.object(settings, "MAX_GENERATIONS_PER_DAY", 100000),
                        patch.object(mws, "send_whatsapp_text", self.text),
                        patch.object(mws, "upload_media_to_meta", self.upload),
                        patch.object(mws, "send_catalog_pack_images_to_whatsapp", self.deliver),
                        patch.object(mws, "_check_failure_rate", lambda db: None),
                        patch.object(mws, "DRY_RUN_IMAGE_MODE", False)]
        for p in self.patches:
            p.start()

    def tearDown(self):
        for p in self.patches:
            p.stop()
        self.tmp.cleanup()
        self.engine.dispose()

    def state(self):
        s = self.Session()
        try:
            row = s.get(WhatsAppIngestion, self.oid)
            return (row.status, sku_packs.balance(s, self.customer_id, SKU_CREATIVE_V1_5),
                    s.get(Customer, self.customer_id).wallet_balance)
        finally:
            s.close()

    def run_worker(self, successes):
        results = ["data:image/png;base64,QUJD" if ok else None for ok in successes]
        with patch.object(mws, "_generate_single_pack_style", AsyncMock(side_effect=results)) as gen:
            ok = asyncio.run(mws.process_whatsapp_catalog_v1_5(self.oid))
        return ok, gen


class WorkerTests(WorkerBase):
    def test_all_five_shots_are_generated_once_each_in_order_and_delivered(self):
        seen = []

        async def fake_style(**kw):
            seen.append((kw["style_title"], kw["prompt"]))
            return "data:image/png;base64,QUJD"

        with patch.object(mws, "_generate_single_pack_style", side_effect=fake_style):
            self.assertTrue(asyncio.run(mws.process_whatsapp_catalog_v1_5(self.oid)))
        self.assertEqual(seen, [(title, prompt) for (title, _), prompt in zip(EXPECTED_STYLES, EXPECTED_PROMPTS)])
        self.assertEqual(self.upload.await_count, 5)
        kwargs = self.deliver.await_args.kwargs
        self.assertEqual(kwargs["image_urls"], [f"media-{i}" for i in range(1, 6)])
        self.assertEqual(kwargs["styles"], mws.CATALOG_V1_5_PACK_STYLES)
        self.assertEqual(kwargs["pack_name"], "Earring Catalog Pack v1.5")
        self.assertEqual(self.state(), ("delivered", 2, 200))               # the credit was used once, wallet untouched

    def test_the_provider_is_called_once_per_style_in_single_attempt_mode(self):
        from app.ai.providers.image_base import ImageGenerationResult

        gen = AsyncMock(return_value=ImageGenerationResult(
            success=True, image_url="data:image/png;base64,QUJD", provider_name="gemini"))
        with patch("app.ai.image_generation_manager.ImageGenerationManager.generate_image", gen):
            self.assertTrue(asyncio.run(mws.process_whatsapp_catalog_v1_5(self.oid)))
        self.assertEqual(gen.await_count, 5)
        for call in gen.await_args_list:
            self.assertIs(call.kwargs["single_attempt"], True)
            self.assertEqual(call.kwargs["reference_image"], self.photo_bytes)
            self.assertEqual(call.kwargs["context"]["aspect_ratio"], "4:5")
        self.assertEqual(sorted(c.kwargs["prompt"] for c in gen.await_args_list), sorted(EXPECTED_PROMPTS))

    def test_four_of_five_is_partial_and_keeps_the_credit_spent(self):
        ok, _ = self.run_worker([True, True, False, True, True])
        self.assertTrue(ok)
        self.assertEqual(self.upload.await_count, 4)
        self.assertEqual(self.state(), ("delivered_partial", 2, 200))
        s = self.Session()
        self.assertEqual(s.get(WhatsAppIngestion, self.oid).error_message, "Delivered 4/5 images")
        s.close()
        self.assertIn("4 of 5 images in your Catalog Pack v1.5", self.text.await_args.args[1])

    def test_only_the_last_shot_failing_is_partial(self):
        self.assertTrue(self.run_worker([True] * 4 + [False])[0])
        self.assertEqual(self.state()[0], "delivered_partial")
        self.assertIn("4 of 5 images", self.text.await_args.args[1])

    def test_every_shot_failing_returns_the_credit_once_and_tells_the_customer(self):
        ok, _ = self.run_worker([False] * 5)
        self.assertFalse(ok)
        self.assertEqual(self.state(), ("failed", 3, 200))                   # credit back, wallet untouched
        self.assertIn("Catalog Pack v1.5", self.text.await_args.args[1])
        self.assertIn("SKU credit has been returned", self.text.await_args.args[1])
        s = self.Session()
        self.assertEqual(s.query(CustomerSkuCredit).filter_by(action=ACTION_REFUND).count(), 1)
        s.close()

    def test_a_second_start_does_nothing(self):
        self.assertTrue(self.run_worker([True] * 5)[0])
        again_ok, gen = self.run_worker([])
        self.assertFalse(again_ok)
        gen.assert_not_awaited()
        self.assertEqual(self.state(), ("delivered", 2, 200))

    def test_the_older_pack_worker_still_uses_its_eight_styles(self):
        seen = []

        async def fake_style(**kw):
            seen.append(kw["style_title"])
            return "data:image/png;base64,QUJD"

        s = self.Session()
        s.query(WhatsAppIngestion).filter_by(id=self.oid).update({"product_code": "PACK_1", "status": "pack_queued"})
        s.commit()
        s.close()
        self.upload.side_effect = [f"media-{i}" for i in range(1, 9)]
        with patch.object(mws, "_generate_single_pack_style", side_effect=fake_style):
            self.assertTrue(asyncio.run(mws.process_whatsapp_catalog_pack(self.oid)))
        self.assertEqual(seen, [title for title, _ in mws.CATALOG_PACK_STYLES])
        self.assertEqual(len(seen), 8)
        self.assertEqual(self.deliver.await_args.kwargs["styles"], mws.CATALOG_PACK_STYLES)
        self.assertEqual(self.deliver.await_args.kwargs["pack_name"], "Earring Catalog Pack")


class WorkerSpendTests(WorkerBase):
    def real_generation(self, cap, already_used=0):
        from app.ai import image_generation_manager as igm
        from app.ai.image_generation_manager import ImageGenerationManager
        from app.ai.providers.image_base import ImageGenerationResult

        provider = AsyncMock(return_value=ImageGenerationResult(
            success=True, image_url="data:image/png;base64,QUJD", provider_name="gemini"))
        fake = type("P", (), {"is_available": True, "supports_reference_image": lambda self: True,
                               "generate_image": provider})()
        with patch.object(settings, "MAX_GENERATIONS_PER_DAY", cap), \
             patch.object(ImageGenerationManager, "_get_provider", lambda self, name: fake), \
             patch.object(ImageGenerationManager, "_get_provider_chain", lambda self: ["gemini"]):
            if already_used:
                self.assertIsNone(igm.reserve_generation_slots(already_used))
            ok = asyncio.run(mws.process_whatsapp_catalog_v1_5(self.oid))
        return ok, provider

    def spend_used(self):
        from app.ai import image_generation_manager as igm
        from app.services import spend_counter
        shared = spend_counter.used(igm.current_spend_day())
        return igm._spend_count if shared is None else shared

    def test_five_slots_are_reserved_and_the_pack_runs_when_the_cap_allows(self):
        ok, provider = self.real_generation(cap=5)
        self.assertTrue(ok)
        self.assertEqual(provider.await_count, 5)
        self.assertEqual(self.spend_used(), 5)
        self.assertEqual(self.state(), ("delivered", 2, 200))

    def test_a_cap_one_short_blocks_the_whole_pack_and_returns_the_credit(self):
        ok, provider = self.real_generation(cap=4)
        self.assertFalse(ok)
        provider.assert_not_awaited()                           # never a 4/5 pack: the reservation is all-or-nothing
        self.assertEqual(self.spend_used(), 0)
        self.assertEqual(self.state(), ("failed", 3, 200))

    def test_a_partly_used_cap_blocks_the_pack_before_any_call(self):
        ok, provider = self.real_generation(cap=5, already_used=1)
        self.assertFalse(ok)
        provider.assert_not_awaited()
        self.assertEqual(self.spend_used(), 1)
        self.assertEqual(self.state(), ("failed", 3, 200))

    def test_the_old_eight_style_cap_is_not_needed_for_v1_5(self):
        ok, provider = self.real_generation(cap=6)              # fewer than Pack 1 needs (8), more than v1.5 needs (5)
        self.assertTrue(ok)
        self.assertEqual(provider.await_count, 5)


class WorkerDriveTests(WorkerBase):
    def run_on_drive(self, drive_ok):
        titles = []
        index = iter(range(1, 6))

        async def to_drive(image_bytes, customer_id, ingestion_id, style_title):
            titles.append(style_title)
            n = next(index)
            return f"{n:03d} - {style_title}.png" if drive_ok[n - 1] else None

        with patch.object(mws, "_generate_single_pack_style", AsyncMock(return_value=b"png-bytes")), \
                patch.object(mws, "_upload_style_to_drive", side_effect=to_drive), \
                patch("app.services.drive_delivery.enabled", return_value=True), \
                patch("app.services.drive_delivery.customer_id_if_ready", return_value="cust-1"), \
                patch("app.services.batch_notify.schedule") as schedule:
            ok = asyncio.run(mws.process_whatsapp_catalog_v1_5(self.oid))
        return ok, titles, schedule

    def test_all_five_go_to_drive_named_after_their_own_styles_and_nothing_goes_to_the_chat(self):
        ok, titles, schedule = self.run_on_drive([True] * 5)
        self.assertTrue(ok)
        self.assertEqual(titles, [title for title, _ in EXPECTED_STYLES])
        self.deliver.assert_not_awaited()
        self.upload.assert_not_awaited()
        self.assertEqual(self.state(), ("delivered", 2, 200))
        s = self.Session()
        self.assertEqual(s.get(WhatsAppIngestion, self.oid).delivery_channel, "drive")
        s.close()
        schedule.assert_called_once_with("cust-1")

    def test_styles_drive_refuses_are_sent_on_whatsapp_and_the_pack_stays_whole(self):
        ok, _, _ = self.run_on_drive([True, False, True, False, True])
        self.assertTrue(ok)
        self.assertEqual(self.upload.await_count, 2)
        self.assertEqual(len(self.deliver.await_args.kwargs["image_urls"]), 2)
        self.assertEqual(self.state(), ("delivered", 2, 200))

    def test_the_drive_file_name_is_the_next_number_and_the_style(self):
        from app.services import drive_layout

        customer_id = "cust-1"
        names = []

        async def fake_upload(path, name, parent, mime):
            names.append(name)
            return {"id": f"f{len(names)}", "link": ""}

        async def fake_folders(_cid):
            return {"images": "root"}

        async def fake_day(_parent, _when=None):
            return "day", "2026-10-10"

        with patch.object(drive_layout, "ensure_customer_folders", fake_folders), \
                patch.object(drive_layout, "_day_folder", fake_day), \
                patch.object(drive_layout.google_drive, "list_children", AsyncMock(side_effect=lambda _d: [
                    {"name": n} for n in names])), \
                patch.object(drive_layout.google_drive, "upload_file", fake_upload):
            for title, _ in mws.CATALOG_V1_5_PACK_STYLES:
                asyncio.run(drive_layout.upload_delivery_image(customer_id, __file__, "image/png", label=title))
        self.assertEqual(names, ["001 - Clean E-Commerce.png", "002 - Close-up on Ear.png",
                                 "003 - Scale Reference.png", "004 - Professional Studio.png",
                                 "005 - Stand Display.png"])


class DeliveryCaptionTests(unittest.TestCase):
    def send(self, count, **kwargs):
        captions = []

        async def fake_send(recipient_id, media_id, caption, reply_to_message_id=None):
            captions.append(caption)
            return True

        with patch.object(mws, "send_image_to_whatsapp", fake_send), patch.object(mws, "DRY_RUN_IMAGE_MODE", False), \
                patch.object(mws, "CATALOG_PACK_SEND_THROTTLE_SECONDS", 0):
            sent = asyncio.run(mws.send_catalog_pack_images_to_whatsapp(
                SENDER, [f"m{i}" for i in range(1, count + 1)], "2 SKUs", **kwargs))
        return sent, captions

    def test_v1_5_captions_count_to_five(self):
        sent, captions = self.send(5, styles=mws.CATALOG_V1_5_PACK_STYLES, pack_name="Earring Catalog Pack v1.5")
        self.assertEqual(sent, 5)
        self.assertEqual(captions[:4], ["1/5 Clean E-Commerce", "2/5 Close-up on Ear", "3/5 Scale Reference",
                                        "4/5 Professional Studio"])
        self.assertEqual(captions[4], "5/5 Stand Display ✨\nHere's your Earring Catalog Pack v1.5 📦\n"
                                      "Remaining balance: 2 SKUs")

    def test_a_partial_v1_5_delivery_counts_what_was_sent(self):
        _, captions = self.send(3, styles=mws.CATALOG_V1_5_PACK_STYLES, pack_name="Earring Catalog Pack v1.5")
        self.assertEqual(captions[0], "1/3 Clean E-Commerce")
        self.assertTrue(captions[2].startswith("3/3 Scale Reference ✨"))

    def test_pack_1_captions_are_unchanged(self):
        sent, captions = self.send(8)
        self.assertEqual(sent, 8)
        self.assertEqual(captions[6], "7/8 Stand Display")
        self.assertEqual(captions[7], "8/8 Luxury Drape ✨\nHere's your Earring Catalog Pack 📦\nRemaining balance: 2 SKUs")


# ── 7. payments ─────────────────────────────────────────────────────────────────────────────────────────────

class PaymentNotesTests(unittest.TestCase):
    def notes_payload(self, **notes):
        base = {"purpose": "sku_pack", "units": "0", "creative_packs": "0"}
        base.update(notes)
        return {"payload": {"payment": {"entity": {"notes": base}}}}

    def test_pack_counts_are_read_as_three_numbers(self):
        self.assertEqual(payment_routes._pack_counts(self.notes_payload(catalog_v1_5_packs="5")), (0, 0, 5))
        self.assertEqual(payment_routes._pack_counts(self.notes_payload(units="20", creative_packs="2",
                                                                         catalog_v1_5_packs="5")), (20, 2, 5))
        self.assertEqual(payment_routes._pack_counts(self.notes_payload(units="20")), (20, 0, 0))

    def test_bad_or_empty_counts_are_parked_never_credited(self):
        for notes in ({"catalog_v1_5_packs": "abc"}, {"catalog_v1_5_packs": "-1"}, {"catalog_v1_5_packs": "99999999"},
                      {}):
            self.assertEqual(payment_routes._pack_counts(self.notes_payload(**notes)), (0, 0, 0), notes)

    def test_a_recharge_is_not_a_pack(self):
        self.assertIsNone(payment_routes._pack_counts({"payload": {"payment": {"entity": {"notes": {}}}}}))


class PaymentLinkTests(unittest.TestCase):
    def create(self, **kwargs):
        post = AsyncMock(return_value={"short_url": "https://rzp.io/l/x", "id": "plink_x"})
        with patch.object(razorpay_service, "_post_payment_link", post), \
                patch.object(razorpay_service, "record_payment_link", MagicMock()):
            url = asyncio.run(razorpay_service.create_pack_payment_link(SENDER, "T", **kwargs))
        return url, post.await_args.args[0] if post.await_args else None

    def test_a_v1_5_link_is_priced_here_and_carries_the_count_in_its_notes(self):
        url, body = self.create(units=0, creative_packs=0, v1_5_packs=5)
        self.assertEqual(url, "https://rzp.io/l/x")
        self.assertEqual(body["amount"], 5 * 350 * 100)
        self.assertEqual(body["notes"]["catalog_v1_5_packs"], "5")
        self.assertEqual((body["notes"]["units"], body["notes"]["creative_packs"]), ("0", "0"))
        self.assertIn("Catalog Pack v1.5 5 SKUs", body["description"])
        self.assertEqual(json.loads(body["notes"]["cart_summary"])["creative_pack_v1_5"], 5)

    def test_a_mixed_link_adds_up(self):
        _, body = self.create(units=20, creative_packs=2, v1_5_packs=5)
        self.assertEqual(body["amount"], (20 * 20 + 2 * 500 + 5 * 350) * 100)

    def test_a_link_without_v1_5_has_no_v1_5_note(self):
        _, body = self.create(units=0, creative_packs=5)
        self.assertNotIn("catalog_v1_5_packs", body["notes"])
        self.assertNotIn("creative_pack_v1_5", json.loads(body["notes"]["cart_summary"]))

    def test_bad_counts_are_refused(self):
        for kwargs in ({"units": 0, "creative_packs": 0, "v1_5_packs": 0}, {"units": 0, "v1_5_packs": -1},
                       {"units": 0, "v1_5_packs": True}, {"units": 0, "v1_5_packs": 10 ** 6}):
            url, body = self.create(**kwargs)
            self.assertIsNone(url, kwargs)
            self.assertIsNone(body)


class PaymentWebhookTests(LedgerTestBase):
    """A Razorpay pack payment with v1.5 SKUs grants them once, beside the other products, and never the wallet."""

    concurrent = True

    def setUp(self):
        super().setUp()
        from fastapi.testclient import TestClient

        from app.main import app

        app.dependency_overrides.clear()

        def _override_get_db():
            yield self.db

        app.dependency_overrides[get_db] = _override_get_db
        self.client = TestClient(app)
        self.addCleanup(app.dependency_overrides.clear)
        self.texts = []

        async def capture_text(recipient_id, message_text, reply_to_message_id=None):
            self.texts.append(message_text)
            return True

        self.invoice = AsyncMock()
        for p in (patch.object(settings, "RAZORPAY_WEBHOOK_SECRET", ""),
                  patch.object(payment_routes, "send_whatsapp_text", new=AsyncMock(side_effect=capture_text)),
                  patch.object(payment_routes, "send_document_to_whatsapp", new=AsyncMock(return_value=True)),
                  patch.object(payment_routes, "generate_invoice_pdf", return_value=b"%PDF"),
                  patch.object(payment_routes, "dispatch_payment_invoice", new=self.invoice),
                  patch.object(sku_packs, "queue_followups", new=MagicMock())):
            p.start()
            self.addCleanup(p.stop)

    def link(self, link_id, rupees, units):
        self.db.add(RazorpayPaymentLink(link_id=link_id, whatsapp_id=SENDER, amount_rupees=rupees,
                                        purpose="sku_pack", units=units))
        self.db.commit()

    def event(self, payment_id, link_id, rupees, **notes):
        base = {"whatsapp_id": SENDER, "purpose": "sku_pack", "units": "0", "creative_packs": "0"}
        base.update(notes)
        payment = {"id": payment_id, "amount": rupees * 100, "currency": "INR", "status": "captured"}
        link = {"id": link_id, "amount": rupees * 100, "currency": "INR", "status": "paid", "notes": base}
        return {"event": "payment_link.paid", "payload": {"payment": {"entity": payment},
                                                          "payment_link": {"entity": link}}}

    def post(self, payload):
        response = self.client.post("/api/payments/razorpay/webhook", content=json.dumps(payload).encode(),
                                    headers={"Content-Type": "application/json"})
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()["status"]

    def balances(self, customer):
        self.db.expire_all()
        return [sku_packs.balance(self.db, customer.id, s) for s in (SKU_WHITE_BG, SKU_CREATIVE, SKU_CREATIVE_V1_5)]

    def test_a_v1_5_payment_grants_v1_5_credits_once_and_leaves_the_wallet_alone(self):
        customer = self.make_customer(300)
        self.link("plink_v15", 1750, 0)
        event = self.event("pay_v15a", "plink_v15", 1750, catalog_v1_5_packs="5")
        self.assertEqual(self.post(event), "ok")
        self.assertEqual(self.post(event), "already_processed")
        self.assertEqual(self.balances(customer), [0, 0, 5])
        self.db.expire_all()
        self.assertEqual(self.db.get(Customer, customer.id).wallet_balance, 300)
        details = json.loads(self.db.query(AuditLog).filter_by(action="razorpay_payment_captured").one().details)
        self.assertEqual((details["amount_paid"], details["purpose"], details["catalog_v1_5_packs"]),
                         (1750, "sku_pack", 5))
        self.assertEqual(len(self.texts), 1)
        self.assertIn("Catalog Pack v1.5 5 SKUs added to your account.", self.texts[0])
        self.assertIn("Catalog Pack v1.5 SKUs available: 5", self.texts[0])
        self.assertEqual(self.invoice.await_count, 1)
        assert_ledger_matches_balances(self, self.db)

    def test_a_mixed_cart_grants_each_product_once(self):
        customer = self.make_customer(0)
        self.link("plink_mix", 20 * 20 + 2 * 500 + 5 * 350, 20)
        self.assertEqual(self.post(self.event("pay_mix", "plink_mix", 3150, units="20", creative_packs="2",
                                              catalog_v1_5_packs="5")), "ok")
        self.assertEqual(self.balances(customer), [20, 2, 5])
        refs = {r.sku: r.reference_id for r in self.db.query(CustomerSkuCredit).filter_by(action=ACTION_PURCHASE)}
        self.assertEqual(refs, {"white_bg": "pay_mix", "creative_pack": "pay_mix:creative_pack",
                                "creative_pack_v1_5": "pay_mix:creative_pack_v1_5"})

    def test_a_payment_that_does_not_match_its_link_is_not_credited(self):
        customer = self.make_customer(0)
        self.link("plink_cheap", 350, 0)                                   # we sold 1 SKU ...
        self.assertEqual(self.post(self.event("pay_cheat", "plink_cheap", 1750, catalog_v1_5_packs="5")),
                         "pack_unverified")                                # ... the notes claim 5 for ₹1,750
        self.assertEqual(self.balances(customer), [0, 0, 0])

    def test_unusable_v1_5_counts_are_parked(self):
        customer = self.make_customer(0)
        self.link("plink_bad", 1750, 0)
        self.assertEqual(self.post(self.event("pay_bad", "plink_bad", 1750, catalog_v1_5_packs="five")),
                         "pack_units_invalid")
        self.assertEqual(self.balances(customer), [0, 0, 0])

    def test_the_receipt_helper_names_the_product(self):
        customer = self.make_customer(0)
        sku_packs.grant_pack_credits(self.db, customer, 5, "pay_r", sku=SKU_CREATIVE_V1_5)
        self.db.commit()
        text = payment_routes._pack_receipt(self.db, customer, 0, 0, 5)
        self.assertIn("Catalog Pack v1.5 5 SKUs added to your account.", text)
        self.assertNotRegex(text, r"Valid till: -")


class WhatsAppPayTests(LedgerTestBase):
    def test_order_lines_add_up_to_the_total_for_every_cart(self):
        offset = whatsapp_pay_service.INR_OFFSET
        for white, creative, v1_5 in ((0, 0, 5), (5, 0, 5), (0, 5, 5), (20, 2, 5)):
            total = (white * 20 + creative * 500 + v1_5 * 350)
            items = whatsapp_pay_service.pack_order_items(white, creative, total, v1_5)
            self.assertEqual(sum(i["amount"]["value"] * i["quantity"] for i in items), total * offset,
                             (white, creative, v1_5))
            self.assertEqual(len(items), (white > 0) + (creative > 0) + 1)

    def test_the_v1_5_line_has_its_tier_id_and_name(self):
        (item,) = whatsapp_pay_service.pack_order_items(0, 0, 1750, 5)
        self.assertEqual((item["retailer_id"], item["name"]), ("sku_pack_v1_5_5", "Catalog Pack v1.5 5 SKUs"))

    def test_carts_without_v1_5_are_built_exactly_as_before(self):
        self.assertEqual(whatsapp_pay_service.pack_order_items(0, 3, 1500)[0]["retailer_id"], "sku_pack_3")
        self.assertEqual(whatsapp_pay_service.pack_order_items(20, 0, 400)[0]["retailer_id"],
                         pricing.pack_retailer_id(20))

    def test_a_cart_that_cannot_add_up_becomes_one_combined_line(self):
        items = whatsapp_pay_service.pack_order_items(5, 0, 100, 5)           # 5 v1.5 SKUs alone cost 1,750
        self.assertEqual(len(items), 1)
        self.assertTrue(items[0]["retailer_id"].startswith("sku_cart_5_0_5"))
        self.assertEqual(items[0]["amount"]["value"] * items[0]["quantity"], 100 * whatsapp_pay_service.INR_OFFSET)

    def test_a_captured_order_grants_v1_5_credits_and_only_those(self):
        customer = self.make_customer(0)
        order = WhatsAppPaymentOrder(reference_id="ref-v15", whatsapp_id=SENDER, amount_rupees=1750, total_paise=175000,
                                     status="created", purpose="sku_pack", white_units=0, creative_packs=0,
                                     catalog_v1_5_packs=5)
        self.db.add(order)
        self.db.commit()
        whatsapp_pay_service._grant_pack_order(self.db, order, "pay_wa")
        self.db.commit()
        self.db.expire_all()
        self.assertEqual([sku_packs.balance(self.db, customer.id, s)
                          for s in (SKU_WHITE_BG, SKU_CREATIVE, SKU_CREATIVE_V1_5)], [0, 0, 5])
        self.assertEqual(self.db.query(CustomerSkuCredit).one().reference_id, "pay_wa:creative_pack_v1_5")

    def test_an_order_with_nothing_in_it_grants_nothing(self):
        self.make_customer(0)
        order = WhatsAppPaymentOrder(reference_id="ref-none", whatsapp_id=SENDER, amount_rupees=1, total_paise=100,
                                     status="created", purpose="sku_pack", white_units=0, creative_packs=0,
                                     catalog_v1_5_packs=0)
        with self.assertRaises(RuntimeError):
            whatsapp_pay_service._grant_pack_order(self.db, order, "pay_none")

    def test_the_native_order_refuses_bad_counts_before_anything_is_recorded(self):
        for args in ((0, 0, 0), (0, 0, -1), (0, 0, True)):
            ok = asyncio.run(whatsapp_pay_service.try_send_native_pack(
                self.db, SENDER, args[0], args[1], 1750, "body", v1_5_packs=args[2]))
            self.assertFalse(ok, args)
        self.assertEqual(self.db.query(WhatsAppPaymentOrder).count(), 0)


class InvoiceTests(unittest.TestCase):
    def test_a_v1_5_purchase_is_one_line_at_its_price(self):
        lines = billing_service.pack_invoice_lines(0, 0, 1750, 5)
        self.assertEqual([line["qty"] for line in lines], [5])
        self.assertEqual(round(sum(line["qty"] * line["rate"] for line in lines)), 1750)
        self.assertIn("Catalog Pack v1.5 SKUs (5 SKUs)", lines[0]["description"])

    def test_a_mixed_purchase_has_a_line_per_product_adding_up_exactly(self):
        lines = billing_service.pack_invoice_lines(20, 2, 20 * 20 + 2 * 500 + 5 * 350, 5)
        self.assertEqual([line["qty"] for line in lines], [20, 2, 5])
        self.assertEqual(round(sum(line["qty"] * line["rate"] for line in lines)), 3150)

    def test_invoices_without_v1_5_are_unchanged(self):
        lines = billing_service.pack_invoice_lines(20, 2, 20 * 20 + 2 * 450)
        self.assertEqual([line["qty"] for line in lines], [20, 2])

    def test_an_impossible_split_is_one_line_never_a_negative_one(self):
        lines = billing_service.pack_invoice_lines(5, 0, 100, 5)
        self.assertEqual(len(lines), 1)
        self.assertGreater(lines[0]["qty"] * lines[0]["rate"], 0)

    def test_the_receipt_text_names_what_was_bought(self):
        self.assertEqual(billing_service.pack_description(0, 0, 5), "Catalog Pack v1.5 5 SKUs")
        self.assertEqual(billing_service.pack_description(5, 0, 5), "5 SKUs (white background) + Catalog Pack v1.5 5 SKUs")
        self.assertEqual(billing_service.pack_description(5, 2), "5 SKUs (white background) + Catalog Pack 2 SKUs")


# ── 8. routing of retries and the outbox ────────────────────────────────────────────────────────────────────

class RoutingTests(LedgerTestBase):
    def test_the_outbox_job_kind_follows_the_product(self):
        self.assertEqual(meta_webhook._outbox_kind(PRODUCT_WHITE_BG), "white")
        self.assertEqual(meta_webhook._outbox_kind(PRODUCT_PACK_1), "pack")
        self.assertEqual(meta_webhook._outbox_kind(PRODUCT_PACK_V1_5), "pack_v1_5")
        self.assertEqual(meta_webhook._outbox_kind(None), "pack")

    def test_a_recorded_order_run_starts_the_right_worker(self):
        for kind, expected in (("white", "process_whatsapp_white_bg"), ("pack_v1_5", "process_whatsapp_catalog_v1_5"),
                               ("pack", "process_whatsapp_catalog_pack"), (None, "process_whatsapp_catalog_pack")):
            mocks = {name: AsyncMock(return_value=True) for name in (
                "process_whatsapp_white_bg", "process_whatsapp_catalog_v1_5", "process_whatsapp_catalog_pack")}
            with patch.multiple(mws, **mocks):
                self.assertTrue(asyncio.run(outbox._handle_order_run({"worker": kind, "ingestion_id": "ing-1"})))
            for name, mock in mocks.items():
                self.assertEqual(mock.await_count, 1 if name == expected else 0, (kind, name))

    def test_the_queue_hands_a_v1_5_order_to_its_own_worker(self):
        tasks = BackgroundTasks()
        with patch.object(settings, "OUTBOX_ENABLED", False):
            meta_webhook._queue_order_run(tasks, (mws.process_whatsapp_catalog_v1_5, "ing-1"))
        self.assertEqual([(t.func, t.args) for t in tasks.tasks], [(mws.process_whatsapp_catalog_v1_5, ("ing-1",))])

    def test_a_failed_v1_5_order_is_retried_by_its_own_worker(self):
        customer = self.make_customer(0)
        sku_packs.grant_pack_credits(self.db, customer, 2, "pay_x", sku=SKU_CREATIVE_V1_5)
        ingestion = self.make_ingestion(status="failed", product_code=PRODUCT_PACK_V1_5, credit_source=CREDIT_SOURCE_SKU)
        sku_packs.consume_credit(self.db, customer, ingestion.id, SKU_CREATIVE_V1_5)
        self.db.commit()
        tasks = BackgroundTasks()
        result = meta_webhook.retry_delivery(ingestion.id, tasks, current_user=None, db=self.db)
        self.assertEqual(result["status"], "queued", result)
        self.assertEqual([(t.func, t.args) for t in tasks.tasks],
                         [(meta_webhook.process_whatsapp_catalog_v1_5, (ingestion.id,))])

    def test_a_failed_pack_1_order_is_still_retried_by_the_pack_1_worker(self):
        self.make_customer(0)
        ingestion = self.make_ingestion(status="failed", product_code=PRODUCT_PACK_1, amount_charged=500)
        tasks = BackgroundTasks()
        result = meta_webhook.retry_delivery(ingestion.id, tasks, current_user=None, db=self.db)
        self.assertEqual(result["status"], "queued", result)
        self.assertEqual([t.func for t in tasks.tasks], [meta_webhook._trigger_generation])

    def test_a_returned_v1_5_credit_makes_a_retry_refused(self):
        customer = self.make_customer(0)
        sku_packs.grant_pack_credits(self.db, customer, 2, "pay_x", sku=SKU_CREATIVE_V1_5)
        ingestion = self.make_ingestion(status="failed", product_code=PRODUCT_PACK_V1_5, credit_source=CREDIT_SOURCE_SKU)
        sku_packs.consume_credit(self.db, customer, ingestion.id, SKU_CREATIVE_V1_5)
        self.db.commit()
        self.assertTrue(sku_packs.refund_credit(self.db, ingestion.id))
        result = meta_webhook.retry_delivery(ingestion.id, BackgroundTasks(), current_user=None, db=self.db)
        self.assertEqual(result["status"], "error")


if __name__ == "__main__":
    unittest.main()
