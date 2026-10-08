"""Phase 8 SKU packs in the chat: list_reply / order parsing, the pack list menu, one payment link per list pick or
catalogue cart (server-computed price), the "packs" keyword, the email ask after a greeting, and today's behaviour
with SKU_PACKS_ENABLED off. All Meta and Razorpay calls are mocked; no network call is made."""

import unittest
from unittest.mock import AsyncMock, patch

from app.config import settings
from app.models.processed_message import ProcessedMessage
from app.models.sku_credit import PRICE_KEY_SKU, PriceSetting
from app.services import meta_whatsapp_service as mws
from app.services import pricing, razorpay_service, sku_messages
from app.services.meta_whatsapp_service import parse_webhook_entry
from tests.test_registration_flow import _payload, _text
from tests.test_wallet_funded_slot_gate import SENDER, FundedSlotGateTestCase, _make_customer

PAY_URL = "https://rzp.io/l/pack"


def _list_reply(row_id, message_id="wamid.list1", sender=SENDER):
    return {"type": "interactive", "id": message_id, "from": sender, "timestamp": "1700000200",
            "interactive": {"type": "list_reply",
                            "list_reply": {"id": row_id, "title": "20 SKUs", "description": "any text"}}}


def _order(items, message_id="wamid.order1", sender=SENDER):
    """A Meta catalogue cart. item_price / currency are deliberately absurd: the server must never use them."""
    return {"type": "order", "id": message_id, "from": sender, "timestamp": "1700000300",
            "order": {"catalog_id": "CAT1", "text": "", "product_items": [
                {"product_retailer_id": rid, "quantity": qty, "item_price": 1, "currency": "USD"} for rid, qty in items
            ]}}


def _pack_settings(**overrides):
    values = dict(SKU_PACKS_ENABLED=True, SKU_PRICE_RUPEES=20, SKU_PACK_SIZES="1,5,20,50,100",
                  ECOM_PACK1_ENABLED=False)
    values.update(overrides)
    return [patch.object(settings, k, v) for k, v in values.items()]


class ParseTests(unittest.TestCase):
    def test_list_reply_is_parsed(self):
        ev = parse_webhook_entry(_payload(_list_reply("pack_20"))["entry"][0])
        self.assertEqual(len(ev), 1)
        self.assertEqual((ev[0]["type"], ev[0]["subtype"], ev[0]["message_id"], ev[0]["sender"], ev[0]["timestamp"]),
                         ("interactive", "list_reply", "wamid.list1", SENDER, "1700000200"))
        self.assertEqual(ev[0]["list_reply"], {"id": "pack_20", "title": "20 SKUs"})

    def test_order_is_parsed_and_its_prices_are_dropped(self):
        ev = parse_webhook_entry(_payload(_order([("pack_20", 2), ("pack_5", 1)]))["entry"][0])
        self.assertEqual(len(ev), 1)
        self.assertEqual((ev[0]["type"], ev[0]["message_id"], ev[0]["sender"], ev[0]["catalog_id"]),
                         ("order", "wamid.order1", SENDER, "CAT1"))
        self.assertEqual(ev[0]["items"], [{"retailer_id": "pack_20", "quantity": 2},
                                          {"retailer_id": "pack_5", "quantity": 1}])

    def test_order_without_items_is_safe(self):
        msg = {"type": "order", "id": "wamid.o", "from": SENDER, "timestamp": "1"}
        ev = parse_webhook_entry(_payload(msg)["entry"][0])
        self.assertEqual((ev[0]["type"], ev[0]["catalog_id"], ev[0]["items"]), ("order", "", []))


class MessageCopyTests(unittest.TestCase):
    def test_copy_uses_the_given_numbers(self):
        self.assertLessEqual(len(sku_messages.PACK_MENU_BUTTON), 20)
        self.assertIn("Total SKUs: 40", sku_messages.pack_link_body(40, 800))
        self.assertTrue(sku_messages.pack_link_body(40, 800).startswith("Your Cart is ready!"))
        self.assertLessEqual(len(sku_messages.PAY_BUTTON), 20)
        self.assertIn(pricing.format_rupees(800), sku_messages.pack_link_body(40, 800))
        self.assertIn("1 SKU used, 13 left", sku_messages.photo_received(13))
        self.assertEqual(sku_messages.ready_message("https://d/x", 13),
                         "Your images are ready! 📁 Download from your Drive: https://d/x. You have 13 SKUs left.")
        self.assertIn("13 SKUs left", sku_messages.buy_more(13))
        self.assertIn("used all", sku_messages.buy_more(0))
        for text in (sku_messages.pack_menu_body(), sku_messages.pack_link_unavailable(), sku_messages.ask_email(),
                     sku_messages.order_unreadable()):
            self.assertTrue(0 < len(text) <= 1024)


class SenderTests(unittest.IsolatedAsyncioTestCase):
    async def _menu(self, **overrides):
        post = AsyncMock(return_value=True)
        patches = _pack_settings(**overrides) + [patch.object(mws, "_post_message_payload", new=post),
                                                 patch.object(pricing, "sku_price", return_value=20)]
        for p in patches:
            p.start()
        try:
            self.assertTrue(await mws.send_pack_menu(SENDER, reply_to_message_id="wamid.q"))
        finally:
            for p in reversed(patches):
                p.stop()
        self.assertEqual(post.await_args.kwargs["reply_to_message_id"], "wamid.q")
        return post.await_args.args[0]

    async def test_menu_rows_respect_meta_limits_and_hide_pack_1(self):
        payload = await self._menu()
        inter = payload["interactive"]
        self.assertEqual((payload["type"], inter["type"]), ("interactive", "list"))
        self.assertEqual(inter["body"]["text"], sku_messages.pack_menu_body())
        self.assertEqual(inter["action"]["button"], sku_messages.PACK_MENU_BUTTON)
        rows = inter["action"]["sections"][0]["rows"]
        self.assertEqual([r["id"] for r in rows], ["pack_5", "pack_20", "pack_50", "pack_100", "creative_pack_1"])
        self.assertEqual(rows[1]["title"], "20 SKUs")
        self.assertIn(pricing.format_rupees(400), rows[1]["description"])          # 20 x 20, computed here
        for r in rows:
            self.assertLessEqual(len(r["title"]), 24)
            self.assertLessEqual(len(r["description"]), 72)

    async def test_pack_1_is_shown_only_when_enabled(self):
        payload = await self._menu(ECOM_PACK1_ENABLED=True)
        rows = payload["interactive"]["action"]["sections"][0]["rows"]
        self.assertEqual(rows[0]["id"], "pack_1")
        self.assertEqual(rows[0]["title"], "1 SKU")

    async def test_list_message_trims_to_meta_limits(self):
        post = AsyncMock(return_value=True)
        rows = [(f"r{i}", "T" * 30, "D" * 90) for i in range(12)]
        with patch.object(mws, "_post_message_payload", new=post):
            self.assertTrue(await mws.send_list_message(SENDER, "Body", "B" * 25, rows))
            self.assertFalse(await mws.send_list_message(SENDER, "Body", "Button", []))
        action = post.await_args.args[0]["interactive"]["action"]
        self.assertEqual(len(action["button"]), 20)
        self.assertEqual(len(action["sections"]), 1)
        self.assertEqual(len(action["sections"][0]["rows"]), 10)
        self.assertEqual({(len(r["title"]), len(r["description"])) for r in action["sections"][0]["rows"]}, {(24, 72)})

    async def test_template_payload(self):
        post = AsyncMock(return_value=True)
        with patch.object(mws, "_post_message_payload", new=post):
            self.assertTrue(await mws.send_whatsapp_template(SENDER, "images_ready", "en", ["https://d/x", "13"]))
        payload = post.await_args.args[0]
        self.assertEqual((payload["to"], payload["type"]), (SENDER, "template"))
        self.assertEqual(payload["template"]["name"], "images_ready")
        self.assertEqual(payload["template"]["language"], {"code": "en"})
        self.assertEqual(payload["template"]["components"],
                         [{"type": "body", "parameters": [{"type": "text", "text": "https://d/x"},
                                                          {"type": "text", "text": "13"}]}])


class PackWebhookBase(FundedSlotGateTestCase):
    packs_enabled = True

    def setUp(self):
        super().setUp()
        self.cta = AsyncMock(return_value=True)
        self.menu = AsyncMock(return_value=True)
        self.pack_link = AsyncMock(return_value=PAY_URL)
        self.recharge_link = AsyncMock(return_value="https://pay")
        self.flow = AsyncMock(return_value=True)
        self.extra = _pack_settings(SKU_PACKS_ENABLED=self.packs_enabled) + [
            patch.object(self.webhook_module, "send_whatsapp_cta_url_button", new=self.cta),
            patch.object(self.webhook_module, "send_pack_menu", new=self.menu),
            patch.object(self.webhook_module, "create_recharge_payment_link", new=self.recharge_link),
            patch.object(self.webhook_module, "send_registration_flow", new=self.flow),
            # Written by the payments work; patched here whether or not it exists yet.
            patch.object(razorpay_service, "create_pack_payment_link", new=self.pack_link, create=True),
        ]
        for p in self.extra:
            p.start()

    def tearDown(self):
        for p in reversed(self.extra):
            p.stop()
        super().tearDown()

    def _post(self, *messages):
        self.assertEqual(self.client.post("/api/meta/webhook", json=_payload(*messages)).status_code, 200)

    def _assert_one_link(self, units, total):
        self.pack_link.assert_awaited_once()
        self.assertEqual(self.pack_link.await_args.kwargs, {"customer_phone": SENDER, "customer_name": "Ananya Shah",
                                                            "units": units, "creative_packs": 0})
        self.cta.assert_awaited_once()
        kwargs = self.cta.await_args.kwargs
        self.assertEqual(kwargs["url"], PAY_URL)
        self.assertEqual(kwargs["button_label"], sku_messages.PAY_BUTTON)
        self.assertEqual(kwargs["body_text"], sku_messages.pack_link_body(units, total))


class PackWebhookTests(PackWebhookBase):
    def test_list_reply_pack_5_sends_one_link_for_5(self):
        _make_customer(self.session, balance=0)
        self._post(_list_reply("pack_5"))
        self._assert_one_link(5, 100)
        self.assertEqual(self.cta.await_args.kwargs["reply_to_message_id"], "wamid.list1")
        self.menu.assert_not_awaited()

    def test_order_2_x_pack_20_is_one_link_for_40(self):
        _make_customer(self.session, balance=0)
        self._post(_order([("pack_20", 2)]))
        self._assert_one_link(40, 800)
        self.assertIn("Total Amount: ₹800 (incl. GST)", self.cta.await_args.kwargs["body_text"])

    def test_order_ignores_unknown_ids_and_bad_quantities(self):
        _make_customer(self.session, balance=0)
        self._post(_order([("pack_20", 1), ("ring_7", 3), ("pack_7", 1), ("pack_5", 0), ("pack_5", -1),
                           ("pack_5", True), ("pack_5", 1.5), ("pack_5", "2"), ("pack_50", None)]))
        self._assert_one_link(30, 600)                    # 20 + 2 x 5

    def test_order_total_is_capped(self):
        _make_customer(self.session, balance=0)
        self._post(_order([("pack_100", 10 ** 9)]))
        self.assertEqual(self.pack_link.await_args.kwargs["units"], 10_000)

    def test_unreadable_order_gets_a_message_and_the_menu_but_no_link(self):
        self._post(_order([("ring_7", 2), ("pack_20", 0)]))
        self.assertEqual(self.sent_texts, [sku_messages.order_unreadable()])
        self.menu.assert_awaited_once_with(SENDER)
        self.pack_link.assert_not_awaited()
        self.cta.assert_not_awaited()

    def test_pack_1_works_by_id_although_hidden_from_the_menu(self):
        self._post(_list_reply("pack_1"))
        self._post(_order([("pack_1", 3)], message_id="wamid.order.p1"))
        self.assertEqual([c.kwargs["units"] for c in self.pack_link.await_args_list], [1, 3])
        self.assertEqual(self.pack_link.await_args_list[0].kwargs["customer_name"], "Customer")   # unknown sender
        self.assertEqual([c.kwargs["body_text"].split("Total Amount: ")[1].split(" ")[0]
                          for c in self.cta.await_args_list], ["₹20", "₹60"])

    def test_other_list_rows_are_ignored(self):
        self._post(_list_reply("pack_7"), _list_reply("something_else", message_id="wamid.list2"))
        self.pack_link.assert_not_awaited()
        self.cta.assert_not_awaited()
        self.menu.assert_not_awaited()
        self.assertEqual(self.sent_texts, [])

    def test_no_link_means_an_apology_and_no_button(self):
        self.pack_link.return_value = None
        self._post(_list_reply("pack_20"))
        self.pack_link.side_effect = RuntimeError("razorpay down")
        self._post(_list_reply("pack_20", message_id="wamid.list2"))
        self.assertEqual(self.sent_texts, [sku_messages.pack_link_unavailable()] * 2)
        self.cta.assert_not_awaited()

    def test_the_stored_price_is_used_never_the_cart_price(self):
        self.session.add(PriceSetting(sku=PRICE_KEY_SKU, price_rupees=25))
        self.session.commit()
        self._post(_order([("pack_20", 2)]))
        self.assertIn("Total Amount: ₹1,000 (incl. GST)", self.cta.await_args.kwargs["body_text"])

    def test_a_duplicate_order_delivery_is_handled_once(self):
        self._post(_order([("pack_20", 2)], message_id="wamid.dup"))
        self._post(_order([("pack_20", 2)], message_id="wamid.dup"))
        self.pack_link.assert_awaited_once()
        self.cta.assert_awaited_once()

    def test_pack_words_send_the_menu(self):
        for i, word in enumerate(("packs", "Pack", "BUY", "menu", "price", " Prices ")):
            self._post(_text(word, message_id=f"wamid.w{i}"))
        self.assertEqual(self.menu.await_count, 6)
        self.assertEqual(self.sent_texts, [])
        self.pack_link.assert_not_awaited()

    def test_unmatched_text_gets_the_menu(self):
        self._post(_text("what do you do?", message_id="wamid.t1"))
        self.menu.assert_awaited_once_with(SENDER)

    def test_recharge_and_email_still_win_over_the_menu(self):
        _make_customer(self.session, balance=0)
        self._post(_text("recharge 500", message_id="wamid.r1"))
        self._post(_text("buy@shop.in", message_id="wamid.e1"))
        self.recharge_link.assert_awaited_once()
        self.menu.assert_not_awaited()

    def test_registered_customer_without_email_is_asked_for_it_once(self):
        _make_customer(self.session, balance=0)
        self._post(_text("hi", message_id="wamid.h1"))
        self.assertEqual(len(self.sent_texts), 3)
        self.assertTrue(self.sent_texts[0].startswith("Welcome"))
        self.assertTrue(self.sent_texts[1].startswith("Quick Setup"))
        self.assertEqual(self.sent_texts[2], sku_messages.ask_email())
        self.menu.assert_not_awaited()

    def test_no_email_ask_with_an_email_or_for_a_new_sender(self):
        cust = _make_customer(self.session, balance=0)
        cust.email = "a@b.in"
        self.session.commit()
        self._post(_text("hi", message_id="wamid.h1"))
        self.session.delete(cust)
        self.session.commit()
        self._post(_text("hi", message_id="wamid.h2"))                # new sender: the registration Flow
        self.assertNotIn(sku_messages.ask_email(), self.sent_texts)
        self.flow.assert_awaited_once()


class PacksOffTests(PackWebhookBase):
    """SKU_PACKS_ENABLED off: everything behaves exactly as before Phase 8."""

    packs_enabled = False

    def _assert_nothing(self):
        self.pack_link.assert_not_awaited()
        self.cta.assert_not_awaited()
        self.menu.assert_not_awaited()
        self.assertEqual(self.sent_texts, [])

    def test_everything_new_is_silent(self):
        _make_customer(self.session, balance=0)
        self._post(_list_reply("pack_5"))
        self._post(_order([("pack_20", 2)]))
        self._post(_order([("ring_7", 1)], message_id="wamid.order2"))
        self._post(_text("packs", message_id="wamid.t1"))
        self._post(_text("what do you do?", message_id="wamid.t2"))
        self._assert_nothing()
        ids = {m.message_id for m in self.session.query(ProcessedMessage).all()}
        self.assertFalse(ids & {"wamid.order1", "wamid.order2"})       # orders are not even claimed

    def test_greeting_is_unchanged(self):
        _make_customer(self.session, balance=0)
        self._post(_text("hi", message_id="wamid.h1"))
        self.assertEqual(len(self.sent_texts), 2)
        self.assertTrue(self.sent_texts[1].startswith("Quick Setup"))


if __name__ == "__main__":
    unittest.main()


class CartTests(PackWebhookBase):
    def test_mixed_cart_is_one_link_with_one_grand_total(self):
        _make_customer(self.session, balance=0)
        self._post(_order([("pack_20", 2), ("pack_5", 1), ("creative_pack_1", 2)]))
        self.pack_link.assert_awaited_once()
        self.assertEqual(self.pack_link.await_args.kwargs["units"], 45)
        self.assertEqual(self.pack_link.await_args.kwargs["creative_packs"], 2)
        body = self.cta.await_args.kwargs["body_text"]
        total = 45 * pricing.sku_price() + 2 * pricing.creative_pack_price()
        self.assertIn("Total SKUs: 45", body)
        self.assertIn(f"Total Amount: {pricing.format_rupees(total)} (incl. GST)", body)

    def test_quote_cart_ignores_meta_prices_and_unknown_items(self):
        quote = pricing.quote_cart([{"retailer_id": "pack_20", "quantity": 2, "item_price": 1},
                                    {"retailer_id": "PACK_5", "quantity": 1},
                                    {"retailer_id": "creative_pack_1", "quantity": "1"},
                                    {"retailer_id": "ring", "quantity": 4}])
        self.assertEqual((quote["white_units"], quote["creative_packs"]), (45, 1))
        self.assertEqual(quote["total"], 45 * pricing.sku_price() + pricing.creative_pack_price())


class JourneyTests(PackWebhookBase):
    def test_45_skus_cost_900_in_one_link(self):
        _make_customer(self.session, balance=0)
        self._post(_order([("pack_20", 2), ("pack_5", 1)]))
        self.assertEqual(self.pack_link.await_args.kwargs["units"], 45)
        self.assertIn("Total Amount: ₹900 (incl. GST)", self.cta.await_args.kwargs["body_text"])

    def test_collections_message_has_one_view_collections_button(self):
        import asyncio

        with patch.object(mws, "send_reply_buttons", new=AsyncMock(return_value=True)) as buttons:
            asyncio.run(mws.send_collections(SENDER))
        recipient, body, rows = buttons.await_args.args
        self.assertEqual(rows, [(mws.COLLECTIONS_BUTTON, "View Collections")])
        self.assertIn("E-commerce Only", body)
        self.assertIn(pricing.CREATIVE_TITLE, body)

    def test_tapping_view_collections_opens_the_catalogue(self):
        tap = {"type": "interactive", "id": "wamid.coll1", "from": SENDER, "timestamp": "1",
               "interactive": {"type": "button_reply", "button_reply": {"id": mws.COLLECTIONS_BUTTON, "title": "View Collections"}}}
        with patch.object(mws, "send_catalogue", new=AsyncMock(return_value=True)) as catalogue:
            self._post(tap)
        catalogue.assert_awaited_once_with(SENDER)

    def test_without_a_catalogue_the_catalogue_is_the_list_menu(self):
        import asyncio

        with patch.object(settings, "META_CATALOG_ID", ""), \
                patch.object(mws, "send_pack_menu", new=AsyncMock(return_value=True)) as menu:
            asyncio.run(mws.send_catalogue(SENDER))
        menu.assert_awaited_once()

    def test_the_welcome_text_is_exact(self):
        self.assertEqual(self.webhook_module.WELCOME_MESSAGE,
                         "Welcome to Moraa Studio ✨\nWe transform your raw jewelry photos into studio-grade product "
                         "visuals in seconds.\nLet’s quickly set up your account!")
