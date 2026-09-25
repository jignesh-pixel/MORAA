"""ERPNext billing: Sales Invoice + Payment Entry + PDF, idempotency, and the
local-receipt fallback. All ERPNext HTTP is served by httpx.MockTransport —
no network call is made."""

import asyncio
import json
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

import httpx

from app.api.routes import payment_routes
from app.config import settings
from app.services import billing_service
from app.services import erpnext_service
from app.services.erpnext_service import ERPNextService
from tests.test_wallet_funded_slot_gate import SENDER, FundedSlotGateTestCase, _make_customer

PDF = b"%PDF-1.7 erpnext invoice"
ERP_SETTINGS = {
    "ERPNEXT_RECHARGE_ITEM_CODE": "GEMVISION-WALLET-RECHARGE",
    "ERPNEXT_TAX_TEMPLATE": "Output GST 18% - M",
    "ERPNEXT_MODE_OF_PAYMENT": "Razorpay",
    "ERPNEXT_PRINT_FORMAT": "Standard",
    "ERPNEXT_PRICES_INCLUDE_TAX": True,
    "ERPNEXT_DEFAULT_DEBTORS_ACCOUNT": "Debtors - M",
    "ERPNEXT_PAYMENT_ACCOUNT": "",
    "ERPNEXT_JOB_TIMEOUT_SECONDS": 10.0,
}


class FakeERPNext:
    """Minimal ERPNext REST double recording every request."""

    def __init__(self, existing_invoice=None, fail=None):
        self.calls = []
        self.existing_invoice = existing_invoice
        self.fail = fail  # (method, path-substring) -> 500, or "timeout"
        self.outstanding = 500.0

    def handler(self, request: httpx.Request) -> httpx.Response:
        path = httpx.URL(str(request.url)).path
        body = json.loads(request.content) if request.content else None
        self.calls.append((request.method, path, dict(request.url.params), body))
        if self.fail == "timeout":
            raise httpx.ReadTimeout("timed out", request=request)
        if self.fail and request.method == self.fail[0] and self.fail[1] in path:
            return httpx.Response(500, json={"exc_type": "ValidationError"})
        if path == "/api/resource/Sales Invoice" and request.method == "GET":
            return httpx.Response(200, json={"data": [self.existing_invoice] if self.existing_invoice else []})
        if path == "/api/resource/Customer" and request.method == "GET":
            return httpx.Response(200, json={"data": []})
        if path == "/api/resource/Customer" and request.method == "POST":
            return httpx.Response(200, json={"data": {"name": "CUST-0001"}})
        if path.startswith("/api/resource/Sales Taxes and Charges Template/"):
            return httpx.Response(200, json={"data": {"taxes": [
                {"charge_type": "On Net Total", "account_head": "Output Tax CGST - M", "rate": 9.0},
                {"charge_type": "On Net Total", "account_head": "Output Tax SGST - M", "rate": 9.0}]}})
        if path == "/api/resource/Sales Invoice" and request.method == "POST":
            self.existing_invoice = {"name": "ACC-SINV-0001", "docstatus": 1, "outstanding_amount": 500.0}
            return httpx.Response(200, json={"data": {"name": "ACC-SINV-0001"}})
        if path.startswith("/api/resource/Sales Invoice/"):
            return httpx.Response(200, json={"data": {"name": path.rsplit("/", 1)[1], "outstanding_amount": self.outstanding}})
        if path == "/api/resource/Payment Entry" and request.method == "GET":
            return httpx.Response(200, json={"data": []})
        if path.endswith("payment_entry.get_payment_entry"):
            return httpx.Response(200, json={"message": {"doctype": "Payment Entry", "paid_amount": 500, "__islocal": 1}})
        if path == "/api/resource/Payment Entry" and request.method == "POST":
            self.outstanding = 0.0
            return httpx.Response(200, json={"data": {"name": "ACC-PAY-0001"}})
        if path == "/api/method/frappe.utils.print_format.download_pdf":
            return httpx.Response(200, content=PDF, headers={"content-type": "application/pdf"})
        return httpx.Response(404, json={})

    def posts(self, fragment):
        return [c for c in self.calls if c[0] == "POST" and fragment in c[1]]


def _service():
    return ERPNextService(base_url="https://erp.test", api_key="k", api_secret="s", company="moraa")


class _ERPTestBase(unittest.TestCase):
    def setUp(self):
        self.patches = [patch.object(settings, k, v) for k, v in ERP_SETTINGS.items()]
        for p in self.patches:
            p.start()

    def tearDown(self):
        for p in reversed(self.patches):
            p.stop()

    def _run(self, fake, coro_factory):
        real = httpx.AsyncClient

        def client_factory(*args, **kwargs):
            kwargs["transport"] = httpx.MockTransport(fake.handler)
            return real(*args, **kwargs)

        with patch.object(erpnext_service.httpx, "AsyncClient", side_effect=client_factory):
            return asyncio.run(coro_factory())


class ERPNextServiceTests(_ERPTestBase):
    def test_creates_submits_pays_and_downloads_invoice(self):
        fake = FakeERPNext()
        result = self._run(fake, lambda: _service().create_paid_invoice_pdf(
            SENDER, "Moraa Jewels", "24AAAPS1234C1Z5", 500, "pay_ABC123"))
        self.assertEqual(result, (PDF, "ACC-SINV-0001"))

        cust = fake.posts("/api/resource/Customer")[0][3]
        self.assertEqual((cust["mobile_no"], cust["customer_name"], cust["gstin"], cust["gst_category"]),
                         (SENDER, "Moraa Jewels", "24AAAPS1234C1Z5", "Registered Regular"))
        inv = fake.posts("/api/resource/Sales Invoice")[0][3]
        self.assertEqual((inv["po_no"], inv["docstatus"], inv["customer"], inv["company"]),
                         ("pay_ABC123", 1, "CUST-0001", "moraa"))
        self.assertEqual(inv["items"], [{"item_code": "GEMVISION-WALLET-RECHARGE", "qty": 1, "rate": 500.0}])
        self.assertEqual(inv["taxes_and_charges"], "Output GST 18% - M")
        self.assertEqual([t["included_in_print_rate"] for t in inv["taxes"]], [1, 1])
        self.assertEqual(inv["debit_to"], "Debtors - M")
        pe = fake.posts("/api/resource/Payment Entry")[0][3]
        self.assertEqual((pe["mode_of_payment"], pe["reference_no"], pe["docstatus"]), ("Razorpay", "pay_ABC123", 1))
        self.assertNotIn("__islocal", pe)
        pdf_call = [c for c in fake.calls if c[1].endswith("download_pdf")][0]
        self.assertEqual((pdf_call[2]["doctype"], pdf_call[2]["name"], pdf_call[2]["format"]),
                         ("Sales Invoice", "ACC-SINV-0001", "Standard"))
        lookup = [c for c in fake.calls if c[0] == "GET" and c[1] == "/api/resource/Sales Invoice"][0]
        self.assertIn(["po_no", "=", "pay_ABC123"], json.loads(lookup[2]["filters"]))

    def test_duplicate_payment_id_reuses_existing_invoice(self):
        fake = FakeERPNext(existing_invoice={"name": "ACC-SINV-0007", "docstatus": 1, "outstanding_amount": 0})
        fake.outstanding = 0.0
        result = self._run(fake, lambda: _service().create_paid_invoice_pdf(SENDER, "X", None, 500, "pay_DUP"))
        self.assertEqual(result, (PDF, "ACC-SINV-0007"))
        self.assertEqual(fake.posts("/api/resource/Sales Invoice"), [])
        self.assertEqual(fake.posts("/api/resource/Customer"), [])
        self.assertEqual(fake.posts("Payment Entry"), [])
        self.assertEqual(fake.posts("get_payment_entry"), [])

    def test_second_run_for_same_payment_does_not_bill_twice(self):
        fake = FakeERPNext()
        svc = _service()
        self._run(fake, lambda: svc.create_paid_invoice_pdf(SENDER, "X", None, 500, "pay_TWICE"))
        self._run(fake, lambda: svc.create_paid_invoice_pdf(SENDER, "X", None, 500, "pay_TWICE"))
        self.assertEqual(len(fake.posts("/api/resource/Sales Invoice")), 1)
        self.assertEqual(len(fake.posts("/api/resource/Payment Entry")), 1)

    def test_api_error_returns_none(self):
        fake = FakeERPNext(fail=("POST", "/api/resource/Sales Invoice"))
        self.assertIsNone(self._run(fake, lambda: _service().create_paid_invoice_pdf(SENDER, "X", None, 500, "pay_E")))

    def test_timeout_returns_none(self):
        fake = FakeERPNext(fail="timeout")
        self.assertIsNone(self._run(fake, lambda: _service().create_paid_invoice_pdf(SENDER, "X", None, 500, "pay_T")))

    def test_not_configured_is_noop(self):
        fake = FakeERPNext()
        svc = ERPNextService(base_url="", api_key="", api_secret="", company="")
        self.assertIsNone(self._run(fake, lambda: svc.create_paid_invoice_pdf(SENDER, "X", None, 500, "pay_N")))
        self.assertEqual(fake.calls, [])

    def test_settings_are_read_from_pydantic_settings(self):
        with patch.object(settings, "ERPNEXT_BASE_URL", "https://erp.from.settings/"), \
             patch.object(settings, "ERPNEXT_API_KEY", "k"), patch.object(settings, "ERPNEXT_API_SECRET", "s"), \
             patch.object(settings, "ERPNEXT_COMPANY", "moraa"):
            svc = ERPNextService()
        self.assertEqual((svc.base_url, svc.is_configured), ("https://erp.from.settings", True))


class BillingDispatchTests(_ERPTestBase):
    def _dispatch(self, erp_result=None, erp_exc=None, enabled=True, send_ok=True):
        local = MagicMock(return_value=b"%PDF local")
        send = AsyncMock(return_value=send_ok)
        svc = MagicMock()
        svc.create_paid_invoice_pdf = AsyncMock(return_value=erp_result, side_effect=erp_exc)
        with patch.object(settings, "ERPNEXT_INVOICE_ENABLED", enabled), \
             patch.object(billing_service, "get_erpnext_service", return_value=svc):
            outcome = asyncio.run(billing_service.dispatch_payment_invoice(
                SENDER, "pay_XYZ9", 500, "Valued Customer",
                {"full_name": "Anurag", "business_name": "Moraa Jewels", "gst_number": "N/A"}, local, send))
        return outcome, local, send, svc

    def test_success_sends_erpnext_pdf_only(self):
        outcome, local, send, svc = self._dispatch(erp_result=(PDF, "ACC-SINV-0001"))
        self.assertEqual(outcome, "erpnext")
        local.assert_not_called()
        self.assertEqual(send.await_args.kwargs["filename"], "ACC-SINV-0001.pdf")
        self.assertEqual(send.await_args.kwargs["document_bytes"], PDF)
        kwargs = svc.create_paid_invoice_pdf.await_args.kwargs
        self.assertEqual((kwargs["customer_name"], kwargs["gstin"], kwargs["payment_id"]), ("Moraa Jewels", None, "pay_XYZ9"))

    def test_erpnext_failure_falls_back_to_local_receipt(self):
        for result, exc in ((None, None), (None, RuntimeError("boom"))):
            outcome, local, send, _ = self._dispatch(erp_result=result, erp_exc=exc)
            self.assertEqual(outcome, "local")
            local.assert_called_once_with(customer_name="Valued Customer", invoice_number="Invoice_MoraaStudio_XYZ9", amount=500)
            self.assertEqual(send.await_args.kwargs["filename"], "Invoice_MoraaStudio_XYZ9.pdf")

    def test_send_failure_of_erpnext_pdf_falls_back(self):
        outcome, local, send, _ = self._dispatch(erp_result=(PDF, "ACC-SINV-0001"), send_ok=False)
        self.assertEqual(outcome, "local")
        self.assertEqual(send.await_count, 2)

    def test_disabled_never_calls_erpnext(self):
        outcome, local, send, svc = self._dispatch(enabled=False)
        self.assertEqual(outcome, "local")
        svc.create_paid_invoice_pdf.assert_not_awaited()

    def test_name_and_gstin_helpers(self):
        self.assertEqual(billing_service.billing_name({"business_name": "Jewelry Business", "full_name": "Valued Customer"}, "91x"), "WhatsApp 91x")
        self.assertEqual(billing_service.billing_name({"business_name": "Jewelry Business", "full_name": "Anurag"}, "91x"), "Anurag")
        self.assertEqual(billing_service.billing_gstin({"gst_number": "24aaaps1234c1z5"}), "24AAAPS1234C1Z5")
        self.assertIsNone(billing_service.billing_gstin({"gst_number": "N/A"}))


class RazorpayWebhookBillingTests(FundedSlotGateTestCase):
    """The webhook credits + sends the receipt text inline; the invoice goes
    out in the background (ERPNext PDF when enabled)."""

    def _payload(self, pay_id="pay_WEBHOOK1"):
        return {"event": "payment.captured", "payload": {"payment": {"entity": {
            "id": pay_id, "amount": 50000, "notes": {"sender_id": SENDER}}}}}

    def test_webhook_credits_then_sends_erpnext_invoice_once(self):
        _make_customer(self.session, balance=100)
        texts, docs = [], []
        svc = MagicMock()
        svc.create_paid_invoice_pdf = AsyncMock(return_value=(PDF, "ACC-SINV-0042"))

        async def text(recipient_id, message_text=None, **_):
            texts.append(message_text)
            return True

        async def doc(recipient_id, document_bytes, filename="", caption="", **_):
            docs.append(filename)
            return True

        with patch.object(settings, "RAZORPAY_WEBHOOK_SECRET", ""), \
             patch.object(settings, "ERPNEXT_INVOICE_ENABLED", True), \
             patch.object(payment_routes, "send_whatsapp_text", new=AsyncMock(side_effect=text)), \
             patch.object(payment_routes, "send_document_to_whatsapp", new=AsyncMock(side_effect=doc)), \
             patch.object(payment_routes, "generate_invoice_pdf", return_value=b"%PDF local"), \
             patch.object(billing_service, "get_erpnext_service", return_value=svc):
            for _ in range(2):  # Razorpay retry of the same payment
                r = self.client.post("/api/payments/razorpay/webhook", json=self._payload())
                self.assertEqual(r.status_code, 200)
        self.assertEqual(self._balance(), 600)
        self.assertEqual(len([t for t in texts if "Payment Received" in t]), 1)
        self.assertEqual(docs, ["ACC-SINV-0042.pdf"])
        self.assertEqual(svc.create_paid_invoice_pdf.await_count, 1)
        self.assertEqual(svc.create_paid_invoice_pdf.await_args.kwargs["customer_name"], "Shah Gems & Jewels")
        self.assertEqual(svc.create_paid_invoice_pdf.await_args.kwargs["gstin"], "24AAAPS1234C1Z5")


if __name__ == "__main__":
    unittest.main()
