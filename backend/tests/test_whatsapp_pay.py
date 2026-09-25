"""Native WhatsApp Pay (order_details) recharge — builder, fallback, payment
status webhook and reconciliation. All Meta / Razorpay calls are mocked."""

import re
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, patch

from app.config import settings
from app.models.audit_log import AuditLog
from app.models.whatsapp_payment_order import WhatsAppPaymentOrder
from app.services import meta_whatsapp_service as mws
from app.services import razorpay_service
from app.services import whatsapp_pay_service as pay
from app.services.meta_whatsapp_service import parse_webhook_entry
from tests.test_wallet_funded_slot_gate import SENDER, FundedSlotGateTestCase, _make_customer

CONFIG = {
    "WHATSAPP_PAY_ENABLED": True,
    "WHATSAPP_PAY_CONFIGURATION_NAME": "moraa_studio_razorpay",
    "WHATSAPP_PAY_IMPORTER_ADDRESS_LINE1": "12 Diamond Plaza",
    "WHATSAPP_PAY_IMPORTER_CITY": "Surat",
    "WHATSAPP_PAY_IMPORTER_ZONE_CODE": "GJ",
    "WHATSAPP_PAY_IMPORTER_POSTAL_CODE": "395003",
}


def _payment_webhook(reference_id, status="captured", tx_status="success", pay_id="pay_TEST123"):
    return {"object": "whatsapp_business_account", "entry": [{"changes": [{"field": "messages", "value": {
        "statuses": [{"id": "gBGG1", "recipient_id": SENDER, "type": "payment", "status": status,
                      "payment": {"reference_id": reference_id, "amount": {"value": 50000, "offset": 100},
                                  "currency": "INR", "transaction": {
                                      "id": "order_RZP1", "pg_transaction_id": pay_id, "type": "razorpay",
                                      "status": tx_status, "method": {"type": "upi"}}},
                      "timestamp": "1758790000"}]}}]}]}


def _lookup(reference_id, status="captured", value=50000, pay_id="pay_TEST123", tx_status="success"):
    return {"reference_id": reference_id, "status": status, "currency": "INR",
            "amount": {"value": value, "offset": 100},
            "transactions": [{"id": "order_RZP1", "pg_transaction_id": pay_id, "type": "razorpay",
                              "status": tx_status, "method": {"type": "upi"}}]}


class WhatsAppPayTests(FundedSlotGateTestCase):
    def setUp(self):
        super().setUp()
        self.post = AsyncMock(return_value=True)
        self.cta = AsyncMock(return_value=True)
        self.link = AsyncMock(return_value="https://rzp.io/fallback")
        self.pay_text = AsyncMock(return_value=True)
        self.doc = AsyncMock(return_value=True)
        self.extra = [
            patch.object(pay, "_post_message_payload", new=self.post),
            patch.object(pay, "send_whatsapp_text", new=self.pay_text),
            patch.object(mws, "send_document_to_whatsapp", new=self.doc),
            patch.object(mws, "send_whatsapp_cta_url_button", new=self.cta),
            patch.object(razorpay_service, "create_recharge_payment_link", new=self.link),
            patch.object(self.webhook_module, "send_whatsapp_cta_url_button", new=self.cta),
            patch.object(self.webhook_module, "create_recharge_payment_link", new=self.link),
        ]
        for p in self.extra:
            p.start()

    def tearDown(self):
        for p in reversed(self.extra):
            p.stop()
        super().tearDown()

    def _enable(self, **overrides):
        values = dict(CONFIG, **overrides)
        patches = [patch.object(settings, k, v) for k, v in values.items()]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)

    def _post(self, payload):
        self.assertEqual(self.client.post("/api/meta/webhook", json=payload).status_code, 200)

    def _recharge_text(self, amount=500, mid="wamid.r1"):
        return {"object": "whatsapp_business_account", "entry": [{"changes": [{"field": "messages", "value": {
            "messages": [{"type": "text", "id": mid, "from": SENDER, "timestamp": "1",
                          "text": {"body": f"recharge {amount}"}}]}}]}]}

    def _order(self):
        self.session.expire_all()
        return self.session.query(WhatsAppPaymentOrder).one()

    # ── builder ─────────────────────────────────────────────────────────
    def test_builder_matches_meta_order_details_shape(self):
        self._enable()
        ref = pay.new_reference_id()
        self.assertLessEqual(len(ref), 35)
        self.assertRegex(ref, r"^[A-Za-z0-9_.\-]+$")
        body = pay.build_order_details_payload(SENDER, SENDER, ref, 500, "Recharge",
                                               datetime.now(timezone.utc) + timedelta(minutes=15))
        inter = body["interactive"]
        self.assertEqual((inter["type"], inter["action"]["name"]), ("order_details", "review_and_pay"))
        p = inter["action"]["parameters"]
        self.assertEqual((p["reference_id"], p["type"], p["currency"]), (ref, "digital-goods", "INR"))
        gw = p["payment_settings"][0]
        self.assertEqual((gw["type"], gw["payment_gateway"]["type"], gw["payment_gateway"]["configuration_name"]),
                         ("payment_gateway", "razorpay", "moraa_studio_razorpay"))
        self.assertEqual(gw["payment_gateway"]["razorpay"]["notes"]["whatsapp_id"], SENDER)
        self.assertEqual(p["total_amount"], {"value": 50000, "offset": 100})
        order = p["order"]
        self.assertEqual(order["status"], "pending")
        self.assertEqual(order["subtotal"], {"value": 50000, "offset": 100})
        self.assertEqual(order["tax"]["value"], 0)
        item = order["items"][0]
        self.assertEqual((item["quantity"], item["amount"]["value"]), (1, 50000))
        self.assertEqual(item["importer_address"]["zone_code"], "GJ")

    # ── dispatch + fallback ─────────────────────────────────────────────
    def test_disabled_by_default_keeps_existing_link_flow(self):
        _make_customer(self.session, balance=0)
        self._post(self._recharge_text())
        self.post.assert_not_awaited()
        self.assertEqual(self.session.query(WhatsAppPaymentOrder).count(), 0)
        self.assertEqual(self.cta.await_count, 1)

    def test_enabled_sends_native_order_and_no_link(self):
        self._enable()
        _make_customer(self.session, balance=0)
        self._post(self._recharge_text(1000))
        self.assertEqual(self.post.await_count, 1)
        self.cta.assert_not_awaited()
        self.link.assert_not_awaited()
        o = self._order()
        self.assertEqual((o.status, o.amount_rupees, o.total_paise, o.whatsapp_id), ("sent", 1000, 100000, SENDER))

    def test_meta_rejection_falls_back_to_razorpay_link(self):
        self._enable()
        _make_customer(self.session, balance=0)
        self.post.return_value = False
        self._post(self._recharge_text())
        self.assertEqual(self._order().status, "dispatch_failed")
        self.assertEqual(self.cta.await_count, 1)
        self.assertEqual(self.cta.await_args.kwargs["url"], "https://rzp.io/fallback")

    def test_incomplete_config_or_not_allowlisted_uses_link(self):
        self._enable(WHATSAPP_PAY_IMPORTER_POSTAL_CODE="")
        _make_customer(self.session, balance=0)
        self._post(self._recharge_text())
        self.post.assert_not_awaited()
        self.assertEqual(self.cta.await_count, 1)
        with patch.object(settings, "WHATSAPP_PAY_IMPORTER_POSTAL_CODE", "395003"), \
             patch.object(settings, "WHATSAPP_PAY_ALLOWLIST", "919999999999"):
            self._post(self._recharge_text(mid="wamid.r2"))
        self.post.assert_not_awaited()
        self.assertEqual(self.cta.await_count, 2)

    def test_unfunded_photo_sends_native_order_instead_of_hold_text(self):
        from tests.test_wallet_funded_slot_gate import _image_payload
        self._enable()
        _make_customer(self.session, balance=0)
        self._post(_image_payload(1))
        self.assertEqual(self.post.await_count, 1)
        self.assertEqual(self.sent_texts, [])
        body = self.post.await_args.args[0]["interactive"]["body"]["text"]
        self.assertIn("is required for an order", body)
        self.assertNotIn("http", body)

    # ── parser ──────────────────────────────────────────────────────────
    def test_parser_emits_payment_status_and_keeps_plain_statuses(self):
        entry = _payment_webhook("mgv_x")["entry"][0]
        entry["changes"][0]["value"]["statuses"].append({"id": "wamid.s", "status": "read", "timestamp": "1"})
        events = parse_webhook_entry(entry)
        self.assertEqual(events[0]["type"], "payment_status")
        self.assertEqual((events[0]["reference_id"], events[0]["pg_payment_id"], events[0]["transaction_status"]),
                         ("mgv_x", "pay_TEST123", "success"))
        self.assertEqual(events[1], {"type": "status", "message_id": "wamid.s", "status": "read", "timestamp": "1"})

    # ── reconciliation ──────────────────────────────────────────────────
    def _sent_order(self, amount=500):
        self._enable()
        _make_customer(self.session, balance=100)
        self._post(self._recharge_text(amount))
        return self._order()

    def test_captured_webhook_credits_once_after_lookup(self):
        o = self._sent_order()
        with patch.object(pay, "lookup_payment", new=AsyncMock(return_value=_lookup(o.reference_id))):
            self._post(_payment_webhook(o.reference_id))
            self._post(_payment_webhook(o.reference_id))  # Meta retry
        self.assertEqual(self._balance(), 600)
        o = self._order()
        self.assertEqual((o.status, o.credited, o.pg_payment_id), ("captured", True, "pay_TEST123"))
        self.assertEqual(self.session.query(AuditLog).filter_by(
            action="razorpay_payment_captured", resource_id="pay_TEST123").count(), 1)
        self.assertEqual(self.pay_text.await_count, 1)  # one receipt
        self.assertIn("Current Balance: ₹600", self.pay_text.await_args.kwargs["message_text"])

    def test_payment_already_credited_by_razorpay_webhook_is_not_double_credited(self):
        o = self._sent_order()
        self.session.add(AuditLog(action="razorpay_payment_captured", resource_id="pay_TEST123",
                                  resource_type="razorpay_payment", status="success"))
        self.session.commit()
        with patch.object(pay, "lookup_payment", new=AsyncMock(return_value=_lookup(o.reference_id))):
            self._post(_payment_webhook(o.reference_id))
        self.assertEqual(self._balance(), 100)
        self.assertEqual(self._order().status, "captured")
        self.pay_text.assert_not_awaited()

    def test_webhook_alone_never_credits_without_lookup_confirmation(self):
        o = self._sent_order()
        with patch.object(pay, "lookup_payment", new=AsyncMock(return_value=None)):
            self._post(_payment_webhook(o.reference_id))
        self.assertEqual(self._balance(), 100)
        with patch.object(pay, "lookup_payment", new=AsyncMock(return_value=_lookup(o.reference_id, status="pending"))):
            self._post(_payment_webhook(o.reference_id, status="pending", tx_status="pending"))
        self.assertEqual((self._balance(), self._order().status), (100, "pending"))

    def test_amount_mismatch_is_not_credited(self):
        o = self._sent_order()
        with patch.object(pay, "lookup_payment", new=AsyncMock(return_value=_lookup(o.reference_id, value=100))):
            self._post(_payment_webhook(o.reference_id))
        self.assertEqual((self._balance(), self._order().status), (100, "amount_mismatch"))

    def test_failed_transaction_sends_fallback_link_once(self):
        o = self._sent_order()
        self.cta.reset_mock()
        for _ in range(2):
            self._post(_payment_webhook(o.reference_id, status="pending", tx_status="failed"))
        o = self._order()
        self.assertEqual((o.status, o.fallback_sent, self._balance()), ("failed", True, 100))
        self.assertEqual(self.cta.await_count, 1)

    def test_unknown_reference_is_ignored(self):
        _make_customer(self.session, balance=100)
        self._post(_payment_webhook("mgv_unknown"))
        self.assertEqual(self._balance(), 100)

    def test_sweep_reconciles_missed_webhooks(self):
        import asyncio
        o = self._sent_order()
        o.created_at = datetime.now(timezone.utc) - timedelta(minutes=10)
        self.session.commit()
        with patch.object(pay, "lookup_payment", new=AsyncMock(return_value=_lookup(o.reference_id))):
            result = asyncio.run(pay.reconcile_pending_orders(self.session))
        self.assertEqual(result, {"credited": 1})
        self.assertEqual(self._balance(), 600)
