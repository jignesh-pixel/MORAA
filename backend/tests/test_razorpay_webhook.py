"""Tests for the Razorpay payment webhook (wallet recharge lifecycle).

Covers:
1. extract_payment_data — safe parsing of 'payment.captured' (Payment Pages)
   and 'payment_link.paid' (Dynamic Links) payloads.
2. Regression: a payload with a missing/malformed ``id`` must NEVER raise
   KeyError (production logged ``KeyError: "'id'"`` through the request
   logging middleware); it must be acknowledged without a 500.
3. Sender resolution priority: notes.sender_id -> notes phone variants ->
   contact -> customer_id.
4. Route-level behaviour for unparseable / missing-phone / duplicate /
   successful payment events (Meta WhatsApp API mocked; no real sends).

No network calls and no AI credits are consumed.
"""

import asyncio
import json
import os
import sys
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool


from tests.db_support import make_engine
import app.models  # noqa: F401 — register every model with Base.metadata
from app.config import settings
from app.database import Base, get_db
from app.models.audit_log import AuditLog
from app.models.customer import Customer
from app.services import wallet_service

SENDER = "919876543210"
PAYMENT_ID = "pay_Qabcdefgh1234"
LINK_ID = "plink_Qlink98765"


def _payment_captured_payload(
    payment_id=PAYMENT_ID,
    amount=50000,
    notes=None,
    contact=None,
    customer_id=None,
):
    """Build a standard Razorpay payment.captured webhook payload."""
    entity = {"id": payment_id, "amount": amount, "currency": "INR", "status": "captured"}
    if notes is not None:
        entity["notes"] = notes
    if contact is not None:
        entity["contact"] = contact
    if customer_id is not None:
        entity["customer_id"] = customer_id
    return {
        "event": "payment.captured",
        "payload": {"payment": {"entity": entity}},
    }


def _payment_link_paid_payload(
    link_id=LINK_ID,
    payment_id=PAYMENT_ID,
    amount=50000,
    notes=None,
):
    """Build a Razorpay payment_link.paid webhook payload."""
    link_entity = {"id": link_id, "amount": amount, "currency": "INR", "status": "paid"}
    payment_entity = {"id": payment_id, "amount": amount, "status": "captured"}
    if notes is not None:
        link_entity["notes"] = notes
    return {
        "event": "payment_link.paid",
        "payload": {
            "payment": {"entity": payment_entity},
            "payment_link": {"entity": link_entity},
        },
    }


class ExtractPaymentDataTests(unittest.TestCase):
    """Pure-parser tests: no DB, no HTTP."""

    def setUp(self):
        from app.api.routes.payment_routes import extract_payment_data

        self.extract = extract_payment_data

    # ── happy paths ────────────────────────────────────────────────────

    def test_payment_captured_with_notes_sender_id(self):
        result = self.extract(
            _payment_captured_payload(notes={"sender_id": SENDER})
        )

        self.assertIsNotNone(result)
        sender_id, amount_rupees, payment_reference = result
        self.assertEqual(sender_id, SENDER)
        self.assertEqual(amount_rupees, 500)
        self.assertEqual(payment_reference, PAYMENT_ID)

    def test_payment_link_paid_uses_link_notes_and_id(self):
        result = self.extract(
            _payment_link_paid_payload(notes={"sender_id": SENDER})
        )

        self.assertIsNotNone(result)
        sender_id, amount_rupees, payment_reference = result
        self.assertEqual(sender_id, SENDER)
        self.assertEqual(amount_rupees, 500)
        self.assertEqual(payment_reference, PAYMENT_ID)

    def test_order_paid_event_shape(self):
        payload = {
            "event": "order.paid",
            "payload": {
                "payment": {
                    "entity": {
                        "id": PAYMENT_ID,
                        "amount": 100000,
                        "notes": {"sender_id": SENDER},
                    }
                },
                "order": {"entity": {"id": "order_Qabc", "amount": 100000}},
            },
        }

        result = self.extract(payload)

        self.assertIsNotNone(result)
        sender_id, amount_rupees, payment_reference = result
        self.assertEqual(sender_id, SENDER)
        self.assertEqual(amount_rupees, 1000)
        self.assertEqual(payment_reference, PAYMENT_ID)

    def test_paise_are_rounded_to_whole_rupees(self):
        result = self.extract(_payment_captured_payload(amount=49999))

        self.assertIsNotNone(result)
        self.assertEqual(result[1], 500)

    # ── sender fallback chain ──────────────────────────────────────────

    def test_sender_falls_back_to_contact(self):
        result = self.extract(
            _payment_captured_payload(notes={"utm": "x"}, contact="+91 98765 43210")
        )

        self.assertIsNotNone(result)
        self.assertEqual(result[0], "+91 98765 43210")

    def test_sender_falls_back_to_customer_id(self):
        result = self.extract(
            _payment_captured_payload(
                notes={}, contact="", customer_id="cust_Qcust123"
            )
        )

        self.assertIsNotNone(result)
        self.assertEqual(result[0], "cust_Qcust123")

    def test_sender_variants_in_notes_are_picked_up(self):
        for key in ("phone", "whatsapp_id", "mobile"):
            result = self.extract(
                _payment_captured_payload(notes={key: SENDER})
            )
            self.assertIsNotNone(result, msg=key)
            self.assertEqual(result[0], SENDER, msg=key)

    def test_missing_sender_returns_empty_string_not_none(self):
        """No phone anywhere: still extract amount+id so the payment is
        recorded and the route reports missing_phone (no silent money loss)."""
        result = self.extract(_payment_captured_payload())

        self.assertIsNotNone(result)
        sender_id, amount_rupees, payment_reference = result
        self.assertEqual(sender_id, "")
        self.assertEqual(amount_rupees, 500)
        self.assertEqual(payment_reference, PAYMENT_ID)

    # ── regression: malformed / missing id must never raise ────────────

    def test_missing_payment_entity_id_returns_none_without_raising(self):
        """THE regression: production crashed with KeyError("'id'")."""
        payload = {
            "event": "payment.captured",
            "payload": {
                "payment": {"entity": {"amount": 50000, "status": "captured"}}
            },
        }

        try:
            result = self.extract(payload)
        except KeyError as e:
            self.fail(f"extract_payment_data raised KeyError on missing id: {e}")

        self.assertIsNone(result)

    def test_entity_id_of_wrong_type_returns_none_without_raising(self):
        for bad_id in ({"nested": "dict"}, ["list"], None, True):
            payload = {
                "event": "payment.captured",
                "payload": {
                    "payment": {"entity": {"id": bad_id, "amount": 50000}}
                },
            }

            try:
                result = self.extract(payload)
            except Exception as e:
                self.fail(f"extract_payment_data raised {type(e).__name__} on id={bad_id!r}: {e}")
            self.assertIsNone(result, msg=repr(bad_id))

    def test_non_dict_payload_shapes_return_none_without_raising(self):
        for bad in (None, [], "payment.captured", 42, ["a", "b"]):
            try:
                result = self.extract(bad)
            except Exception as e:
                self.fail(f"extract_payment_data raised {type(e).__name__} on {bad!r}")
            self.assertIsNone(result, msg=repr(bad))

    def test_malformed_nested_structures_return_none_without_raising(self):
        cases = [
            {"payload": "not-a-dict"},
            {"payload": {"payment": "not-a-dict"}},
            {"payload": {"payment": {"entity": "not-a-dict"}}},
            {"payload": {"payment": {"entity": {"id": None, "amount": 50000}}}},
            {"payload": {"payment": {"entity": {"id": PAYMENT_ID}}}},  # no amount
            {
                "payload": {
                    "payment": {
                        "entity": {"id": PAYMENT_ID, "amount": "not-a-number"}
                    }
                }
            },
            {
                "payload": {
                    "payment": {
                        "entity": {"id": PAYMENT_ID, "amount": ["list", "amount"]}
                    }
                }
            },
            {
                "payload": {
                    "payment": {
                        "entity": {"id": PAYMENT_ID, "amount": 0}
                    }
                }
            },
            {
                "payload": {
                    "payment": {
                        "entity": {"id": PAYMENT_ID, "amount": -100}
                    }
                }
            },
            {},  # no payload key at all
        ]

        for payload in cases:
            try:
                result = self.extract(payload)
            except Exception as e:
                self.fail(
                    f"extract_payment_data raised {type(e).__name__} "
                    f"on {payload!r}: {e}"
                )
            self.assertIsNone(result, msg=repr(payload))

    def test_notes_of_wrong_type_do_not_crash(self):
        payload = _payment_captured_payload(amount=50000)
        payload["payload"]["payment"]["entity"]["notes"] = "i-am-a-string"
        payload["payload"]["payment"]["entity"]["contact"] = 9876543210

        result = self.extract(payload)

        self.assertIsNotNone(result)
        self.assertEqual(result[0], "9876543210")


class RazorpayWebhookRouteTests(unittest.TestCase):
    """Route-level tests with the Meta WhatsApp API mocked."""

    def setUp(self):
        from fastapi.testclient import TestClient

        from app.api.routes import payment_routes as webhook_module
        from app.main import app

        self.webhook_module = webhook_module
        self.app = app

        engine = make_engine()
        Base.metadata.create_all(bind=engine)
        self.engine = engine
        self.session = sessionmaker(bind=engine)()

        def _override_get_db():
            yield self.session

        app.dependency_overrides[get_db] = _override_get_db
        self.client = TestClient(app)

        self.sent_texts = []
        self.sent_docs = []

        async def capture_text(recipient_id, message_text, reply_to_message_id=None):
            self.sent_texts.append((recipient_id, message_text))
            return True

        async def capture_document(recipient_id, document_bytes, filename="", caption="", reply_to_message_id=None):
            self.sent_docs.append((recipient_id, filename))
            return True

        fake_upload_response = MagicMock()
        fake_upload_response.status_code = 200
        fake_upload_response.json.return_value = {"id": "media_invoice_1"}

        class _FakeMediaClient:
            async def __aenter__(self):
                return self

            async def __aexit__(self, *exc):
                return False

            async def post(self, url, **kwargs):
                return fake_upload_response

        self.patches = [
            patch.object(settings, "RAZORPAY_WEBHOOK_SECRET", ""),
            patch.object(
                webhook_module,
                "send_whatsapp_text",
                new=AsyncMock(side_effect=capture_text),
            ),
            patch.object(
                webhook_module,
                "send_document_to_whatsapp",
                new=AsyncMock(side_effect=capture_document),
            ),
            patch.object(
                webhook_module,
                "generate_invoice_pdf",
                return_value=b"%PDF-1.4 fake invoice",
            ),
            patch(
                "app.services.meta_whatsapp_service.httpx.AsyncClient",
                new=lambda *a, **kw: _FakeMediaClient(),
            ),
        ]
        for patcher in self.patches:
            patcher.start()

    def tearDown(self):
        for patcher in reversed(self.patches):
            patcher.stop()
        self.app.dependency_overrides.clear()
        self.session.close()
        self.engine.dispose()

    def _post(self, payload):
        return self.client.post(
            "/api/payments/razorpay/webhook",
            content=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"},
        )

    # ── regression: the exact production failure shape ─────────────────

    def test_payment_entity_without_id_is_acknowledged_not_500(self):
        """Production logged KeyError("'id'") → 500 through the middleware.

        The route must instead acknowledge the event so Razorpay does not
        retry forever, and must NOT credit any wallet.
        """
        payload = {
            "event": "payment.captured",
            "payload": {
                "payment": {"entity": {"amount": 50000, "status": "captured"}}
            },
        }

        response = self._post(payload)

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"status": "unparseable"})
        self.assertEqual(self.session.query(Customer).count(), 0)
        self.assertEqual(self.session.query(AuditLog).count(), 0)

    def test_non_object_json_body_is_rejected_not_500(self):
        response = self.client.post(
            "/api/payments/razorpay/webhook",
            content=json.dumps(["not", "an", "object"]).encode("utf-8"),
            headers={"Content-Type": "application/json"},
        )

        self.assertEqual(response.status_code, 400)

    # ── normal lifecycle ───────────────────────────────────────────────

    def test_unrelated_events_are_ignored(self):
        response = self._post({"event": "payment.authorized", "payload": {}})

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["status"], "ignored")

    def test_payment_without_sender_phone_reports_missing_phone(self):
        response = self._post(_payment_captured_payload())

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"status": "missing_phone"})
        self.assertEqual(self.session.query(Customer).count(), 0)

    def test_formatted_payer_phone_credits_the_existing_wallet(self):
        """Spaces/hyphens in the payer's number must not create a second wallet."""
        self.session.add(Customer(
            whatsapp_id="919000000004", full_name="Test", business_name="Test Gems",
            gst_number="N/A", address="Surat", wallet_balance=100, is_registered=True,
        ))
        self.session.commit()
        response = self._post(_payment_captured_payload(notes={"sender_id": "+91 90000-00004"}))

        self.assertEqual(response.json(), {"status": "ok"})
        self.session.expire_all()
        self.assertEqual(wallet_service.get_balance(self.session, "919000000004"), 600)
        self.assertEqual(self.session.query(Customer).count(), 1)

    def _audits(self, action):
        return (
            self.session.query(AuditLog)
            .filter(AuditLog.action == action, AuditLog.resource_id == PAYMENT_ID)
            .all()
        )

    def test_payment_without_phone_is_recorded_for_manual_credit_once(self):
        for _ in range(3):  # Razorpay re-delivers the same event
            response = self._post(_payment_captured_payload())
            self.assertEqual(response.json(), {"status": "missing_phone"})

        rows = self._audits("razorpay_payment_unmatched")
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].status, "pending")
        self.assertEqual(json.loads(rows[0].details)["amount_paid"], 500)
        self.assertEqual(self._audits("razorpay_payment_captured"), [])

    def test_phoneless_event_after_credit_is_not_flagged_unmatched(self):
        self._post(_payment_captured_payload(notes={"sender_id": SENDER}))
        response = self._post(_payment_captured_payload())

        self.assertEqual(response.json(), {"status": "already_processed"})
        self.assertEqual(self._audits("razorpay_payment_unmatched"), [])

    def test_transient_credit_failure_returns_503_and_retry_credits_once(self):
        self.session.add(Customer(
            whatsapp_id=SENDER, full_name="Test", business_name="Test Gems",
            gst_number="N/A", address="Surat", wallet_balance=100, is_registered=True,
        ))
        self.session.commit()
        payload = _payment_captured_payload(notes={"sender_id": SENDER})

        with patch.object(self.webhook_module, "credit_wallet", side_effect=RuntimeError("db blip")):
            response = self._post(payload)
        self.assertEqual(response.status_code, 503)
        self.session.expire_all()
        self.assertEqual(self._audits("razorpay_payment_captured"), [])  # claim rolled back
        self.assertEqual(wallet_service.get_balance(self.session, SENDER), 100)

        retry = self._post(payload)  # Razorpay's automatic retry
        self.assertEqual((retry.status_code, retry.json()), (200, {"status": "ok"}))
        self.assertEqual(wallet_service.get_balance(self.session, SENDER), 600)
        self.assertEqual(len(self._audits("razorpay_payment_captured")), 1)

        again = self._post(payload)
        self.assertEqual(again.json(), {"status": "already_processed"})
        self.assertEqual(wallet_service.get_balance(self.session, SENDER), 600)
        # The failure record from the first attempt is closed by the credit.
        self.assertEqual({r.status for r in self._audits("razorpay_payment_credit_failed")}, {"resolved"})

    def test_unmatched_alert_resolved_when_a_later_event_credits(self):
        self._post(_payment_captured_payload())  # no phone: pending alert row
        self.assertEqual([r.status for r in self._audits("razorpay_payment_unmatched")], ["pending"])

        credited = self._post(_payment_captured_payload(notes={"sender_id": SENDER}))
        self.assertEqual(credited.json(), {"status": "ok"})
        self.session.expire_all()
        self.assertEqual([r.status for r in self._audits("razorpay_payment_unmatched")], ["resolved"])

    def test_permanent_credit_failure_stops_retrying_after_the_cap(self):
        self.session.add(Customer(
            whatsapp_id=SENDER, full_name="Test", business_name="Test Gems",
            gst_number="N/A", address="Surat", wallet_balance=100, is_registered=True,
        ))
        self.session.commit()
        payload = _payment_captured_payload(notes={"sender_id": SENDER})
        cap = self.webhook_module.MAX_CREDIT_ATTEMPTS

        with patch.object(self.webhook_module, "credit_wallet", side_effect=RuntimeError("always broken")):
            codes = [self._post(payload).status_code for _ in range(cap)]
        self.assertEqual(codes[:-1], [503] * (cap - 1))
        self.assertEqual(codes[-1], 200)  # Razorpay stops retrying; ALERT for manual credit
        self.session.expire_all()
        self.assertEqual(len(self._audits("razorpay_payment_credit_failed")), cap)
        self.assertEqual(self._audits("razorpay_payment_captured"), [])
        self.assertEqual(wallet_service.get_balance(self.session, SENDER), 100)

    def test_successful_payment_creates_customer_and_credits_wallet(self):
        response = self._post(
            _payment_captured_payload(notes={"sender_id": SENDER}, amount=100000)
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"status": "ok"})

        customers = self.session.query(Customer).all()
        self.assertEqual(len(customers), 1)
        self.assertEqual(customers[0].whatsapp_id, SENDER)
        self.assertEqual(customers[0].wallet_balance, 1000)

        # Exactly one idempotency audit row keyed by the payment id.
        audits = (
            self.session.query(AuditLog)
            .filter(AuditLog.action == "razorpay_payment_captured")
            .all()
        )
        self.assertEqual(len(audits), 1)
        self.assertEqual(audits[0].resource_id, PAYMENT_ID)

        # One "Payment Received" receipt (with the updated balance) + invoice.
        self.assertEqual(len(self.sent_texts), 1)
        self.assertIn("Payment Received 💳", self.sent_texts[0][1])
        self.assertEqual(len(self.sent_docs), 1)
        self.assertEqual(self.sent_docs[0][0], SENDER)
        self.assertIn(".pdf", self.sent_docs[0][1])

    def test_duplicate_payment_is_not_credited_twice(self):
        self._post(_payment_captured_payload(notes={"sender_id": SENDER}))
        self._post(_payment_captured_payload(notes={"sender_id": SENDER}))

        customers = self.session.query(Customer).all()
        self.assertEqual(len(customers), 1)
        self.assertEqual(customers[0].wallet_balance, 500)

    def test_existing_customer_balance_is_increased(self):
        customer = Customer(
            whatsapp_id=SENDER,
            full_name="Ananya Shah",
            business_name="Shah Gems & Jewels",
            gst_number="24AAAPS1234C1Z5",
            address="12, Diamond Plaza, Surat",
            wallet_balance=500,
            is_registered=True,
        )
        self.session.add(customer)
        self.session.commit()

        response = self._post(
            _payment_captured_payload(notes={"sender_id": SENDER})
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"status": "ok"})
        self.session.refresh(customer)
        self.assertEqual(customer.wallet_balance, 1000)
        self.assertEqual(self.session.query(Customer).count(), 1)

    def test_invalid_signature_is_rejected(self):
        with patch.object(settings, "RAZORPAY_WEBHOOK_SECRET", "real-secret"):
            response = self.client.post(
                "/api/payments/razorpay/webhook",
                content=json.dumps(
                    _payment_captured_payload(notes={"sender_id": SENDER})
                ).encode("utf-8"),
                headers={
                    "Content-Type": "application/json",
                    "X-Razorpay-Signature": "deadbeef",
                },
            )

        self.assertEqual(response.status_code, 400)
        self.assertEqual(self.session.query(Customer).count(), 0)


class WalletCreditStillWorksTests(unittest.TestCase):
    """Guard the downstream helper the webhook relies on."""

    def setUp(self):
        engine = make_engine()
        Base.metadata.create_all(bind=engine)
        self.engine = engine
        self.session = sessionmaker(bind=engine)()

    def tearDown(self):
        self.session.close()
        self.engine.dispose()

    def test_credit_wallet_adds_balance(self):
        customer = Customer(
            whatsapp_id=SENDER,
            full_name="Ananya Shah",
            business_name="Shah Gems & Jewels",
            gst_number="24AAAPS1234C1Z5",
            address="12, Diamond Plaza, Surat",
            wallet_balance=0,
            is_registered=True,
        )
        self.session.add(customer)
        self.session.commit()

        balance = wallet_service.credit_wallet(self.session, SENDER, 1500)

        self.assertEqual(balance, 1500)
        self.session.refresh(customer)
        self.assertEqual(customer.wallet_balance, 1500)


if __name__ == "__main__":
    unittest.main()
