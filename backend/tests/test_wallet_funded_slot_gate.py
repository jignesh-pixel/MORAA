"""P0·01 verification — the WhatsApp funded-slot wallet gate.

Exercises the CURRENT meta_webhook.py architecture directly over HTTP
(upload → immediate atomic deduction → catalog-pack background task; no
style-selection step). This intentionally does not use the legacy
`send_prompt_selection_buttons` / `process_whatsapp_generation` fixtures in
test_customer_journey_scenarios.py — those describe an earlier, now-removed
webhook architecture and already fail against current code independent of
this change.

Covers the balances called out by the P0·01 punch-list item (₹0, ₹300,
₹699, ₹700, ₹1400 against the default ₹500 price) plus: no charge on
insufficient balance, correct deduction on success, refund-not-double-charge
on a post-deduction failure, and no duplicate deduction on a retried
(duplicate) webhook delivery.

All Meta API calls and the AI quality guard are mocked; no real WhatsApp API
call is made and no AI credits are consumed.
"""

import os
import sys
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import app.models  # noqa: F401 — register every model with Base.metadata
from app.database import Base, get_db
from app.models.customer import Customer
from app.models.whatsapp_ingestion import WhatsAppIngestion
from app.services import wallet_service

SENDER = "919876543210"


def _make_engine_and_session():
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(bind=engine)
    return engine, sessionmaker(bind=engine)()


def _make_customer(db, balance):
    customer = Customer(
        whatsapp_id=SENDER,
        full_name="Ananya Shah",
        business_name="Shah Gems & Jewels",
        gst_number="24AAAPS1234C1Z5",
        address="12, Diamond Plaza, Surat",
        wallet_balance=balance,
        is_registered=True,
    )
    db.add(customer)
    db.commit()
    db.refresh(customer)
    return customer


def _image_payload(count=1, sender=SENDER, prefix="wamid.gate"):
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
        "entry": [{"changes": [{"field": "messages", "value": {"messages": messages}}]}],
    }


class FundedSlotGateTestCase(unittest.TestCase):
    """Shared webhook harness with all Meta/AI calls mocked."""

    def setUp(self):
        from fastapi.testclient import TestClient

        from app.api.routes import meta_webhook as webhook_module
        from app.main import app

        self.webhook_module = webhook_module
        self.engine, self.session = _make_engine_and_session()
        self.app = app

        def _override_get_db():
            yield self.session

        app.dependency_overrides[get_db] = _override_get_db
        self.client = TestClient(app)

        self.sent_texts = []

        async def capture_text(recipient_id, message_text=None, reply_to_message_id=None, **_):
            self.sent_texts.append(message_text)
            return True

        fake_upload = MagicMock()
        fake_upload.id = "image-record-1"
        fake_upload_service = MagicMock()
        fake_upload_service.process_upload = AsyncMock(return_value=fake_upload)

        self.patches = [
            patch.object(webhook_module, "send_whatsapp_text", new=AsyncMock(side_effect=capture_text)),
            patch.object(webhook_module, "get_media_url", new=AsyncMock(return_value="https://cdn/img.jpg")),
            patch.object(
                webhook_module,
                "download_media",
                new=AsyncMock(return_value=(b"\xff\xd8\xff" + b"\x00" * 64, "image/jpeg")),
            ),
            patch.object(webhook_module, "validate_image", return_value=(True, None)),
            patch.object(
                webhook_module,
                "check_image_quality",
                new=AsyncMock(return_value=MagicMock(approved=True)),
            ),
            patch.object(webhook_module, "UploadService", return_value=fake_upload_service),
            # The 7-style generation itself is out of scope for this gate —
            # never let the background task actually run it.
            patch.object(webhook_module, "process_whatsapp_catalog_pack", new=AsyncMock(return_value=True)),
        ]
        for patcher in self.patches:
            patcher.start()

    def tearDown(self):
        for patcher in reversed(self.patches):
            patcher.stop()
        self.app.dependency_overrides.clear()
        self.session.close()
        self.engine.dispose()

    def _balance(self):
        return wallet_service.get_balance(self.session, SENDER)

    def _statuses(self):
        return [
            row.status
            for row in self.session.query(WhatsAppIngestion)
            .order_by(WhatsAppIngestion.external_message_id)
            .all()
        ]


class BalancePointTests(FundedSlotGateTestCase):
    """The exact balances called out by the P0·01 punch-list item."""

    def test_zero_balance_blocks_and_charges_nothing(self):
        _make_customer(self.session, balance=0)
        response = self.client.post("/api/meta/webhook", json=_image_payload())

        self.assertEqual(response.status_code, 200)
        self.assertEqual(self._statuses(), [])
        self.assertEqual(self._balance(), 0)
        self.assertTrue(any("recharge" in (t or "").lower() for t in self.sent_texts))

    def test_below_one_image_worth_blocks_and_charges_nothing(self):
        _make_customer(self.session, balance=300)
        response = self.client.post("/api/meta/webhook", json=_image_payload())

        self.assertEqual(response.status_code, 200)
        self.assertEqual(self._statuses(), [])
        self.assertEqual(self._balance(), 300)

    def test_699_funds_exactly_one_image_and_leaves_199(self):
        _make_customer(self.session, balance=699)
        response = self.client.post("/api/meta/webhook", json=_image_payload())

        self.assertEqual(response.status_code, 200)
        self.assertEqual(self._statuses(), ["pack_queued"])
        self.assertEqual(self._balance(), 199)

    def test_700_funds_exactly_one_image_and_leaves_200(self):
        _make_customer(self.session, balance=700)
        response = self.client.post("/api/meta/webhook", json=_image_payload())

        self.assertEqual(response.status_code, 200)
        self.assertEqual(self._statuses(), ["pack_queued"])
        self.assertEqual(self._balance(), 200)

    def test_1400_funds_two_images_and_leaves_400(self):
        _make_customer(self.session, balance=1400)
        response = self.client.post("/api/meta/webhook", json=_image_payload(count=2))

        self.assertEqual(response.status_code, 200)
        self.assertEqual(self._statuses(), ["pack_queued", "pack_queued"])
        self.assertEqual(self._balance(), 400)

    def test_1400_with_three_images_funds_two_and_holds_one(self):
        _make_customer(self.session, balance=1400)
        response = self.client.post("/api/meta/webhook", json=_image_payload(count=3))

        self.assertEqual(response.status_code, 200)
        # Only the two funded images become ingestions; the third is held.
        self.assertEqual(self._statuses(), ["pack_queued", "pack_queued"])
        self.assertEqual(self._balance(), 400)


class RefundAndRetryTests(FundedSlotGateTestCase):
    def test_failed_generation_dependency_refunds_and_does_not_double_charge(self):
        """A failure after deduction (e.g. media download) refunds exactly once."""
        _make_customer(self.session, balance=700)

        with patch.object(self.webhook_module, "download_media", new=AsyncMock(return_value=None)):
            response = self.client.post("/api/meta/webhook", json=_image_payload())

        self.assertEqual(response.status_code, 200)
        # Charged then refunded — balance is exactly back to where it started.
        self.assertEqual(self._balance(), 700)
        self.assertEqual(self._statuses(), [])

    def test_duplicate_webhook_delivery_does_not_double_charge(self):
        """Retried/duplicate Meta deliveries of the same message must never re-charge."""
        _make_customer(self.session, balance=700)
        payload = _image_payload(prefix="wamid.dup")

        first = self.client.post("/api/meta/webhook", json=payload)
        second = self.client.post("/api/meta/webhook", json=payload)

        self.assertEqual(first.status_code, 200)
        self.assertEqual(second.status_code, 200)
        self.assertEqual(self._statuses(), ["pack_queued"])
        self.assertEqual(self._balance(), 200)

    def test_charge_is_race_safe_against_a_concurrently_drained_balance(self):
        """charge_customer_balance must decline rather than overspend."""
        customer = _make_customer(self.session, balance=700)

        charged_first, balance_after_first = wallet_service.charge_customer_balance(
            self.session, customer, 500
        )
        charged_second, balance_after_second = wallet_service.charge_customer_balance(
            self.session, customer, 500
        )

        self.assertTrue(charged_first)
        self.assertEqual(balance_after_first, 200)
        self.assertFalse(charged_second)
        self.assertEqual(balance_after_second, 200)


if __name__ == "__main__":
    unittest.main()
