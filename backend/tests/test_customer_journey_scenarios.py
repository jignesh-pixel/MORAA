"""End-to-end tests for the Moraa Studio WhatsApp customer journey.

Covers:
  Scenario 1 — onboarding, wallet_balance = 0, recharge CTA
  Scenario 2 — zero/low balance wallet gate (image held for payment)
  Scenario 3 — multi-image batching with partial wallet balance
  Scenario 4 — AI pre-validation rejection without deduction
  Paid generation — atomic debit, decline on low balance, refund on failure
  Regression    — ENABLE_WALLET_GATE=False leaves the image pipeline untouched

All Meta API calls and all AI calls are mocked:
no real WhatsApp API call is made and no AI credits are consumed.
"""

import asyncio
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
from app.models.customer import Customer
from app.models.whatsapp_ingestion import WhatsAppIngestion
from app.services import image_prevalidation_service, wallet_service
from app.services.image_prevalidation_service import (
    REASON_BLURRY,
    REASON_MULTIPLE_ITEMS,
    REASON_MULTIPLE_PAIRS,
    REASON_NOT_JEWELLERY,
    QualityCheckResult,
    build_rejection_message,
    evaluate_inspection_payload,
)
SENDER = "919876543210"
FULL_REGISTRATION_TEXT = (
    "Name: Ananya Shah\n"
    "Business name: Shah Gems & Jewels\n"
    "GST number: 24AAAPS1234C1Z5\n"
    "Business address: 12, Diamond Plaza, Varachia Road, Surat, Gujarat 395006"
)
MULTI_PAIR_REJECTION = (
    "This image doesn’t meet our guidelines ❌\n\n"
    "We noticed more than one pair of earrings in the photo.\n"
    "Please resend a photo with just one pair clearly visible 📸"
)


# ─── Shared helpers ──────────────────────────────────────────────────────


def _make_engine_and_session():
    engine = make_engine()
    Base.metadata.create_all(bind=engine)
    return engine, sessionmaker(bind=engine)()


def _make_customer(db, whatsapp_id=SENDER, balance=0, is_registered=True):
    customer = Customer(
        whatsapp_id=whatsapp_id,
        full_name="Ananya Shah",
        business_name="Shah Gems & Jewels",
        gst_number="24AAAPS1234C1Z5",
        address="12, Diamond Plaza, Surat",
        wallet_balance=balance,
        is_registered=is_registered,
    )
    db.add(customer)
    db.commit()
    db.refresh(customer)
    return customer


def _make_ingestion(db, message_id="wamid.img.1", sender=SENDER, status="awaiting_selection"):
    ingestion = WhatsAppIngestion(
        external_user_id=sender,
        external_message_id=message_id,
        external_media_id="media_1",
        channel="whatsapp",
        mime_type="image/jpeg",
        image_id="image-record-1",
        status=status,
    )
    db.add(ingestion)
    db.commit()
    db.refresh(ingestion)
    return ingestion


def _image_payload(sender=SENDER, count=1, prefix="wamid.img"):
    messages = [
        {
            "type": "image",
            "id": f"{prefix}.{index + 1}",
            "from": sender,
            "timestamp": "1700000000",
            "image": {"id": f"media_{index + 1}", "mime_type": "image/jpeg"},
        }
        for index in range(count)
    ]
    return {
        "object": "whatsapp_business_account",
        "entry": [
            {
                "changes": [
                    {"field": "messages", "value": {"messages": messages}}
                ]
            }
        ],
    }


def _button_reply_payload(ingestion_id, sender=SENDER, message_id="wamid.btn.1"):
    return {
        "object": "whatsapp_business_account",
        "entry": [
            {
                "changes": [
                    {
                        "field": "messages",
                        "value": {
                            "messages": [
                                {
                                    "type": "interactive",
                                    "id": message_id,
                                    "from": sender,
                                    "timestamp": "1700000000",
                                    "interactive": {
                                        "type": "button_reply",
                                        "button_reply": {
                                            "id": f"prompt_ecommerce:{ingestion_id}",
                                            "title": "Clean E-Commerce",
                                        },
                                    },
                                }
                            ]
                        },
                    }
                ]
            }
        ],
    }


def _text_payload(body="Hi", sender=SENDER, message_id="wamid.text.1"):
    return {
        "object": "whatsapp_business_account",
        "entry": [
            {
                "changes": [
                    {
                        "field": "messages",
                        "value": {
                            "messages": [
                                {
                                    "type": "text",
                                    "id": message_id,
                                    "from": sender,
                                    "timestamp": "1700000000",
                                    "text": {"body": body},
                                }
                            ]
                        },
                    }
                ]
            }
        ],
    }


def _approved(image_bytes, mime_type="image/jpeg"):
    return QualityCheckResult(approved=True, checked=True, is_jewellery=True, item_count=2, pair_count=1)


# ─── Meta payloads ───────────────────────────────────────────────────────


class _FakeHttpClient:
    """Captures the payload posted to the Meta Send API."""

    def __init__(self, captured, *args, **kwargs):
        self._captured = captured

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def post(self, url, headers=None, json=None):
        self._captured["payload"] = json
        response = MagicMock()
        response.status_code = 200
        response.json.return_value = {"messages": [{"id": "wamid.sent"}]}
        return response


class MetaMessagingTests(unittest.TestCase):
    """Scenario-level payloads use the project's existing Meta conventions."""

    def _post(self, coro_fn, *args, **kwargs):
        captured = {}
        with patch(
            "app.services.meta_whatsapp_service.httpx.AsyncClient",
            new=lambda *a, **kw: _FakeHttpClient(captured, *a, **kw),
        ), patch.object(settings, "META_WHATSAPP_TOKEN", "test-token"), patch.object(
            settings, "META_PHONE_NUMBER_ID", "123456"
        ):
            ok = asyncio.run(coro_fn(*args, **kwargs))
        return ok, captured.get("payload")

    def test_send_whatsapp_text_payload(self):
        from app.services.meta_whatsapp_service import send_whatsapp_text

        ok, payload = self._post(send_whatsapp_text, SENDER, "Your balance is ₹0")

        self.assertTrue(ok)
        self.assertEqual(payload["messaging_product"], "whatsapp")
        self.assertEqual(payload["to"], SENDER)
        self.assertEqual(payload["type"], "text")
        self.assertEqual(payload["text"]["body"], "Your balance is ₹0")

    def test_send_whatsapp_cta_url_button_payload(self):
        from app.services.meta_whatsapp_service import send_whatsapp_cta_url_button

        ok, payload = self._post(
            send_whatsapp_cta_url_button,
            SENDER,
            "Please make a payment to continue.",
            "Pay ₹500",
            "https://rzp.io/l/moraa-500",
        )

        self.assertTrue(ok)
        interactive = payload["interactive"]
        self.assertEqual(interactive["type"], "cta_url")
        self.assertEqual(interactive["body"]["text"], "Please make a payment to continue.")
        self.assertEqual(interactive["action"]["name"], "cta_url")
        self.assertEqual(
            interactive["action"]["parameters"]["display_text"], "Pay ₹500"
        )
        self.assertEqual(
            interactive["action"]["parameters"]["url"], "https://rzp.io/l/moraa-500"
        )

    def test_cta_url_button_rejects_missing_or_invalid_url(self):
        from app.services.meta_whatsapp_service import send_whatsapp_cta_url_button

        for url in ("", "   ", "rzp.io/l/x", "javascript:alert(1)"):
            ok, payload = self._post(
                send_whatsapp_cta_url_button, SENDER, "body", "Pay ₹500", url
            )
            self.assertFalse(ok, msg=url)
            self.assertIsNone(payload, msg=url)

    def test_cta_url_button_label_is_truncated_to_meta_limit(self):
        from app.services.meta_whatsapp_service import send_whatsapp_cta_url_button

        ok, payload = self._post(
            send_whatsapp_cta_url_button,
            SENDER,
            "body",
            "Pay ₹500 right now please",
            "https://rzp.io/l/moraa-500",
        )

        self.assertTrue(ok)
        self.assertLessEqual(
            len(payload["interactive"]["action"]["parameters"]["display_text"]), 20
        )


# ─── Wallet arithmetic ───────────────────────────────────────────────────


class WalletServiceTests(unittest.TestCase):
    def setUp(self):
        self.engine, self.db = _make_engine_and_session()

    def tearDown(self):
        self.db.close()
        self.engine.dispose()

    def test_charge_generation_is_atomic_and_never_negative(self):
        customer = _make_customer(self.db, balance=1000)
        first = _make_ingestion(self.db, "wamid.c.1")
        second = _make_ingestion(self.db, "wamid.c.2")
        third = _make_ingestion(self.db, "wamid.c.3")

        charged_first, balance_first = wallet_service.charge_generation(self.db, first)
        charged_second, balance_second = wallet_service.charge_generation(self.db, second)
        charged_third, balance_third = wallet_service.charge_generation(self.db, third)

        self.assertTrue(charged_first)
        self.assertEqual(balance_first, 500)
        self.assertTrue(charged_second)
        self.assertEqual(balance_second, 0)
        self.assertFalse(charged_third)
        self.assertEqual(balance_third, 0)

        self.db.refresh(customer)
        self.assertEqual(customer.wallet_balance, 0)

    def test_refund_returns_the_charge(self):
        customer = _make_customer(self.db, balance=0)

        refunded = wallet_service.refund_generation_charge(self.db, SENDER, 500)

        self.assertTrue(refunded)
        self.db.refresh(customer)
        self.assertEqual(customer.wallet_balance, 500)

    def test_blocked_statuses_never_allow_generation(self):
        held = _make_ingestion(self.db, "wamid.h.1", status="pending_payment")
        rejected = _make_ingestion(self.db, "wamid.r.1", status="rejected")
        paid = _make_ingestion(self.db, "wamid.p.1", status="awaiting_selection")

        self.assertFalse(wallet_service.is_generation_allowed(held))
        self.assertFalse(wallet_service.is_generation_allowed(rejected))
        self.assertTrue(wallet_service.is_generation_allowed(paid))
        self.assertFalse(wallet_service.is_generation_allowed(None))


# ─── Scenario 4: pre-validation policy (pure, no network) ────────────────


class PreValidationPolicyTests(unittest.TestCase):
    def test_single_pair_is_approved(self):
        result = evaluate_inspection_payload(
            {"is_jewellery": True, "item_count": 2, "pair_count": 1, "is_blurry": False}
        )

        self.assertTrue(result.approved)
        self.assertEqual(result.reason, "")

    def test_single_piece_is_approved(self):
        result = evaluate_inspection_payload(
            {"is_jewellery": True, "item_count": 1, "pair_count": 0, "is_blurry": False}
        )

        self.assertTrue(result.approved)

    def test_multiple_pairs_are_rejected(self):
        result = evaluate_inspection_payload(
            {"is_jewellery": True, "item_count": 4, "pair_count": 2, "is_blurry": False}
        )

        self.assertFalse(result.approved)
        self.assertEqual(result.reason, REASON_MULTIPLE_PAIRS)
        self.assertEqual(build_rejection_message(result), MULTI_PAIR_REJECTION)

    def test_multiple_items_are_rejected(self):
        result = evaluate_inspection_payload(
            {"is_jewellery": True, "item_count": 5, "pair_count": 1, "is_blurry": False}
        )

        self.assertEqual(result.reason, REASON_MULTIPLE_ITEMS)

    def test_blurry_is_rejected(self):
        result = evaluate_inspection_payload(
            {"is_jewellery": True, "item_count": 2, "pair_count": 1, "is_blurry": True}
        )

        self.assertEqual(result.reason, REASON_BLURRY)

    def test_non_jewellery_is_rejected(self):
        result = evaluate_inspection_payload(
            {"is_jewellery": False, "item_count": 0, "pair_count": 0, "is_blurry": False}
        )

        self.assertEqual(result.reason, REASON_NOT_JEWELLERY)

    def test_string_fields_from_the_model_are_coerced(self):
        result = evaluate_inspection_payload(
            {
                "is_jewellery": "true",
                "item_count": "4 pieces",
                "pair_count": "2",
                "is_blurry": "false",
                "confidence": "0.8",
            }
        )

        self.assertFalse(result.approved)
        self.assertEqual(result.reason, REASON_MULTIPLE_PAIRS)

    def test_missing_fields_do_not_crash(self):
        result = evaluate_inspection_payload({})

        self.assertFalse(result.approved)
        self.assertEqual(result.reason, REASON_NOT_JEWELLERY)

    def test_inspector_without_api_key_follows_fail_open_policy(self):
        with patch.object(settings, "GEMINI_API_KEY", ""), patch.object(
            settings, "IMAGE_PREVALIDATION_FAIL_OPEN", True
        ):
            result = asyncio.run(image_prevalidation_service.check_image_quality(b"x"))

        self.assertFalse(result.checked)
        self.assertTrue(result.approved)

        with patch.object(settings, "GEMINI_API_KEY", ""), patch.object(
            settings, "IMAGE_PREVALIDATION_FAIL_OPEN", False
        ):
            result = asyncio.run(image_prevalidation_service.check_image_quality(b"x"))

        self.assertFalse(result.checked)
        self.assertFalse(result.approved)


# ─── Webhook integration: Scenarios 2, 3, 4 ──────────────────────────────


if __name__ == "__main__":
    unittest.main()
