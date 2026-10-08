"""The WhatsApp journey, end to end in the webhook: one onboarding message, strict GSTIN format check (with or without
registry verification), photo -> "View Collections" list -> collection products (SKU tiers) -> cart, photos held until
a pack is paid and then started with the new SKUs, and the "Dear ..." line in the Drive ready message. All Meta,
Razorpay and generation calls are mocked; no network call is made."""

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

from fastapi import BackgroundTasks

from app.config import settings
from app.models.customer import Customer
from app.models.whatsapp_ingestion import CREDIT_SOURCE_SKU, WhatsAppIngestion
from app.services import gst_service, pricing, sku_messages, sku_packs, usage_log
from app.services import meta_whatsapp_service as mws
from tests.test_pack_menu import PackWebhookBase, _list_reply, _session_factory
from tests.test_registration_flow import _flow_message, _text
from tests.test_wallet_funded_slot_gate import SENDER, _image_payload, _make_customer

BAD_GSTIN = "INVAILD123"
GOOD_GSTIN = "24AAAPS1234C1Z5"
FLOW = {"flow_token": "moraa_registration_v1", "full_name": "Anurag Mehta", "business_name": "Moraa Jewels",
        "address": "Surat", "email": "anurag@example.com"}


def _button(button_id, message_id="wamid.btn1"):
    return {"type": "interactive", "id": message_id, "from": SENDER, "timestamp": "1700000400",
            "interactive": {"type": "button_reply", "button_reply": {"id": button_id, "title": "x"}}}


class _JourneyBase(PackWebhookBase):
    def setUp(self):
        super().setUp()
        self.collections = AsyncMock(return_value=True)
        self.products = AsyncMock(return_value=True)
        self.buttons = AsyncMock(return_value=True)
        for p in [
            patch.object(self.webhook_module, "send_collections", new=self.collections),
            patch.object(self.webhook_module, "send_collection_products", new=self.products),
            patch.object(mws, "send_reply_buttons", new=self.buttons),
            # gst_service sends its prompts through the service module itself.
            patch.object(mws, "send_whatsapp_text", new=self.webhook_module.send_whatsapp_text),
            patch.object(settings, "GST_VERIFICATION_ENABLED", False),
        ]:
            p.start()
            self.addCleanup(p.stop)

    def _cust(self):
        self.session.expire_all()
        return self.session.query(Customer).filter_by(whatsapp_id=SENDER).one()


class GstinFormatTests(_JourneyBase):
    """Step 2: an invalid GSTIN never completes registration, even with registry verification switched off."""

    def _assert_retry_prompt(self):
        self.buttons.assert_awaited_once()
        _recipient, body, buttons = self.buttons.await_args.args
        self.assertTrue(body.startswith("Invalid GST format. A valid GSTIN must be 15 alphanumeric characters"))
        self.assertTrue(body.endswith("Would you like to re-enter it or skip for now?"))
        self.assertEqual([title for _id, title in buttons], ["Re-enter GSTIN", "Skip for now"])

    def test_flow_with_an_invalid_gstin_asks_again_and_does_not_finish(self):
        self._post(_flow_message({**FLOW, "gst_number": BAD_GSTIN}))
        cust = self._cust()
        self.assertFalse(cust.is_registered)
        self.assertEqual(cust.gst_number, "N/A")
        self._assert_retry_prompt()
        self.collections.assert_not_awaited()                     # no "You're all set"
        self.cta.assert_not_awaited()
        self.assertTrue(gst_service.is_awaiting_gstin(self.session, SENDER))

    def test_skip_for_now_then_finishes_registration_once(self):
        self._post(_flow_message({**FLOW, "gst_number": BAD_GSTIN}))
        self._post(_button(gst_service.BTN_GST_SKIP))
        self.assertTrue(self._cust().is_registered)
        self.collections.assert_awaited_once()
        self.assertIn("You're all set, Anurag!", self.collections.await_args.kwargs["intro"])
        self._post(_button(gst_service.BTN_GST_SKIP, message_id="wamid.btn2"))      # a double tap
        self.collections.assert_awaited_once()

    def test_a_valid_gstin_typed_after_re_enter_finishes_registration(self):
        self._post(_flow_message({**FLOW, "gst_number": BAD_GSTIN}))
        self._post(_button(gst_service.BTN_GST_REENTER))
        self.assertIn(gst_service.REENTER_PROMPT, self.sent_texts)
        self._post(_text(GOOD_GSTIN, message_id="wamid.gst"))
        cust = self._cust()
        self.assertEqual((cust.is_registered, cust.gst_number, cust.is_gst_verified), (True, GOOD_GSTIN, False))
        self.collections.assert_awaited_once()

    def test_another_invalid_answer_asks_again(self):
        self._post(_flow_message({**FLOW, "gst_number": BAD_GSTIN}))
        self._post(_text("12345", message_id="wamid.gst2"))
        self.assertEqual(self.buttons.await_count, 2)
        self.assertFalse(self._cust().is_registered)
        self.collections.assert_not_awaited()

    def test_a_valid_gstin_finishes_at_once_without_a_lookup_vendor(self):
        self._post(_flow_message({**FLOW, "gst_number": GOOD_GSTIN}))
        cust = self._cust()
        self.assertEqual((cust.is_registered, cust.gst_number), (True, GOOD_GSTIN))
        self.buttons.assert_not_awaited()
        self.collections.assert_awaited_once()

    def test_no_gstin_finishes_at_once(self):
        self._post(_flow_message({**FLOW, "gst_number": ""}))
        self.assertTrue(self._cust().is_registered)
        self.collections.assert_awaited_once()

    def test_text_registration_with_an_invalid_gstin_is_held_too(self):
        self._post(_text(f"Name: Anurag\nBrand Name: Moraa\nCity: Surat\nGSTIN (Optional): {BAD_GSTIN}",
                         message_id="wamid.reg"))
        self.assertFalse(self._cust().is_registered)
        self._assert_retry_prompt()
        self.collections.assert_not_awaited()


class CollectionTests(_JourneyBase):
    """Steps 3 and 4: the collections list and each collection's products."""

    def test_picking_a_collection_sends_its_products(self):
        _make_customer(self.session, balance=0)
        self._post(_list_reply(mws.COLLECTION_STUDIO))
        self.products.assert_awaited_once_with(SENDER, mws.COLLECTION_STUDIO, "wamid.list1")
        self.pack_link.assert_not_awaited()

    def test_collections_are_ignored_with_packs_off(self):
        _make_customer(self.session, balance=0)
        with patch.object(settings, "SKU_PACKS_ENABLED", False):
            self._post(_list_reply(mws.COLLECTION_CATALOG))
        self.products.assert_not_awaited()


class CollectionProductsMessageTests(PackWebhookBase):
    def _send(self, collection_id, catalog_id="CAT1"):
        post = AsyncMock(return_value=True)
        menu = AsyncMock(return_value=True)
        with patch.object(settings, "META_CATALOG_ID", catalog_id), patch.object(settings, "ECOM_PACK1_ENABLED", True), \
                patch.object(mws, "_post_message_payload", new=post), patch.object(mws, "send_pack_menu", new=menu), \
                patch("app.database.SessionLocal", _session_factory(self)):
            asyncio.run(mws.send_collection_products(SENDER, collection_id))
        return post, menu

    def test_studio_shot_lists_every_sku_tier_from_the_catalogue(self):
        post, _menu = self._send(mws.COLLECTION_STUDIO)
        interactive = post.await_args.args[0]["interactive"]
        self.assertEqual(interactive["type"], "product_list")
        self.assertEqual(interactive["header"], {"type": "text", "text": "Studio Shot"})
        self.assertEqual(interactive["action"]["catalog_id"], "CAT1")
        items = [i["product_retailer_id"] for i in interactive["action"]["sections"][0]["product_items"]]
        self.assertEqual(items, ["studio_sku_1", "studio_sku_5", "studio_sku_20", "studio_sku_50", "studio_sku_100"])
        body = interactive["body"]["text"]
        for size in (1, 5, 20, 50, 100):
            self.assertIn(f"{pricing.pack_title(size)}: {pricing.format_rupees(pricing.pack_total(size))}", body)

    def test_catalog_pack_lists_its_five_sku_tiers(self):
        post, _menu = self._send(mws.COLLECTION_CATALOG)
        interactive = post.await_args.args[0]["interactive"]
        self.assertEqual(interactive["header"]["text"], "Catalog Pack")
        items = [i["product_retailer_id"] for i in interactive["action"]["sections"][0]["product_items"]]
        self.assertEqual(items, ["sku_pack_1", "sku_pack_5", "sku_pack_20", "sku_pack_50", "sku_pack_100"])
        for size in (1, 5, 20, 50, 100):
            self.assertIn(f"{pricing.pack_title(size)}: {pricing.format_rupees(size * 500)}",
                          interactive["body"]["text"])

    def test_without_a_catalogue_the_list_menu_is_sent(self):
        post, menu = self._send(mws.COLLECTION_STUDIO, catalog_id="")
        post.assert_not_awaited()
        menu.assert_awaited_once()

    def test_the_cart_button_is_place_order(self):
        self.assertEqual(sku_messages.PAY_BUTTON, "Place Order")
        self.assertTrue(sku_messages.pack_link_body(45, 900).startswith("Your Cart is ready!"))


class HeldPhotoTests(_JourneyBase):
    """Step 3: a photo from a customer without SKUs is kept and answered with ONE "View Collections" list;
    step 6: a pack payment starts the held photos with the new SKUs."""

    def _photo(self, index):
        self.assertEqual(self.client.post("/api/meta/webhook",
                                          json=_image_payload(1, prefix=f"wamid.held{index}")).status_code, 200)

    def _rows(self):
        self.session.expire_all()
        return self.session.query(WhatsAppIngestion).order_by(WhatsAppIngestion.created_at,
                                                               WhatsAppIngestion.external_message_id).all()

    def test_photos_without_skus_are_held_and_get_one_collections_list(self):
        _make_customer(self.session, balance=0)
        self._photo(1)
        self._photo(2)
        self.assertEqual([r.status for r in self._rows()], ["awaiting_choice", "awaiting_choice"])
        self.collections.assert_awaited_once()
        self.assertEqual(self.collections.await_args.kwargs["intro"], sku_messages.photos_held())
        self.webhook_module.send_product_selection_buttons.assert_not_awaited()     # no Studio Shot / Catalog Pack buttons

    def test_a_photo_after_the_burst_gets_the_list_again(self):
        from datetime import datetime, timedelta, timezone

        _make_customer(self.session, balance=0)
        self._photo(1)
        self._rows()[0].created_at = datetime.now(timezone.utc) - timedelta(hours=2)
        self.session.commit()
        self._photo(2)
        self.assertEqual(self.collections.await_count, 2)

    def test_wallet_money_does_not_bring_back_the_old_buttons(self):
        _make_customer(self.session, balance=5000)
        self._photo(1)
        self.collections.assert_awaited_once()
        self.webhook_module.send_product_selection_buttons.assert_not_awaited()

    def test_a_pack_payment_starts_held_photos_as_far_as_the_skus_go(self):
        customer = _make_customer(self.session, balance=0)
        self._photo(1)
        self._photo(2)
        sku_packs.grant_pack_credits(self.session, customer, 1, "pay_held1")
        self.session.commit()
        with patch.object(self.webhook_module, "generation_capacity_blocked", new=MagicMock(return_value=None)), \
                patch.object(settings, "OUTBOX_ENABLED", False), \
                patch.object(usage_log, "queue_sync", new=MagicMock()):
            started = asyncio.run(self.webhook_module.start_held_photos(self.session, SENDER, BackgroundTasks()))
        self.assertEqual(started, 1)
        first, second = self._rows()
        self.assertEqual((first.status, first.credit_source), ("white_queued", CREDIT_SOURCE_SKU))
        self.assertEqual(second.status, "awaiting_choice")
        self.assertEqual(sku_packs.balance(self.session, customer.id), 0)
        self.assertIn(sku_messages.held_photos_started(1, 1, 0), self.sent_texts)
        self.assertEqual(self.collections.await_count, 2)          # the first photo, then "add more" for the second

    def test_capacity_trouble_leaves_photos_and_skus_alone(self):
        customer = _make_customer(self.session, balance=0)
        self._photo(1)
        sku_packs.grant_pack_credits(self.session, customer, 1, "pay_held2")
        self.session.commit()
        with patch.object(self.webhook_module, "generation_capacity_blocked", new=MagicMock(return_value="busy")), \
                patch.object(self.webhook_module._mws, "DRY_RUN_IMAGE_MODE", False):
            started = asyncio.run(self.webhook_module.start_held_photos(self.session, SENDER, BackgroundTasks()))
        self.assertEqual(started, 0)
        self.assertEqual([r.status for r in self._rows()], ["awaiting_choice"])
        self.assertEqual(sku_packs.balance(self.session, customer.id), 1)

    def test_nothing_happens_with_packs_off(self):
        with patch.object(settings, "SKU_PACKS_ENABLED", False):
            self.assertEqual(asyncio.run(self.webhook_module.start_held_photos(self.session, SENDER)), 0)


class ReadyMessageTests(PackWebhookBase):
    def test_with_an_email_the_message_names_the_customer_and_the_address(self):
        text = sku_messages.ready_message("https://d/x", 13, "Anurag Mehta", "anurag@example.com")
        self.assertEqual(text, "Dear Anurag, your images have been sent to your email anurag@example.com.\n\n"
                               "Your images are ready! 📁 Download from your Drive: https://d/x. You have 13 SKUs left.")

    def test_without_an_email_it_is_the_plain_ready_line(self):
        self.assertEqual(sku_messages.ready_message("https://d/x", 1),
                         "Your images are ready! 📁 Download from your Drive: https://d/x. You have 1 SKU left.")

