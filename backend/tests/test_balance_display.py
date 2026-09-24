"""Balance shown to the customer is always read from the DB at send time.

Regression for the 24 Sep real test: the payment confirmation echoed the
amount paid as "Current balance", and the product buttons showed a balance
read ~11 s earlier (before a payment that committed during the AI pre-check).
"""

import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from sqlalchemy.orm import sessionmaker

from app.config import settings
from app.database import get_db
from app.models.customer import Customer
from app.services import wallet_service
from tests.test_wallet_funded_slot_gate import (
    SENDER,
    FundedSlotGateTestCase,
    _image_payload,
    _make_customer,
    _make_engine_and_session,
)


class GetBalanceIsAuthoritativeTests(unittest.TestCase):
    def test_reads_db_not_a_stale_cached_object(self):
        engine, db = _make_engine_and_session()
        _make_customer(db, balance=1500)
        cached = db.query(Customer).filter_by(whatsapp_id=SENDER).one()
        other = sessionmaker(bind=engine)()  # another request (e.g. payment webhook)
        wallet_service.credit_wallet(other, SENDER, 500)
        self.assertEqual(cached.wallet_balance, 1500)          # this session's cached copy is stale
        self.assertEqual(wallet_service.get_balance(db, SENDER), 2000)  # the display source is not
        self.assertEqual(wallet_service.get_balance(db, "unknown"), 0)
        other.close()
        db.close()
        engine.dispose()


class PaymentConfirmationShowsRealBalanceTests(unittest.TestCase):
    def test_message_shows_paid_amount_and_new_balance(self):
        from fastapi.testclient import TestClient
        from app.api.routes import payment_routes
        from app.main import app

        engine, db = _make_engine_and_session()
        _make_customer(db, balance=1500)

        def _override():
            yield db

        app.dependency_overrides[get_db] = _override
        texts = []

        async def capture(recipient_id, message_text=None, **_):
            texts.append(message_text)
            return True

        payload = {"event": "payment.captured", "payload": {"payment": {"entity": {
            "id": "pay_display1", "amount": 50000, "notes": {"sender_id": SENDER}}}}}
        try:
            with patch.object(settings, "RAZORPAY_WEBHOOK_SECRET", ""), \
                 patch.object(settings, "DEBUG", True), \
                 patch.object(settings, "RATE_LIMIT_ENABLED", False), \
                 patch.object(payment_routes, "send_whatsapp_text", new=AsyncMock(side_effect=capture)), \
                 patch.object(payment_routes, "send_document_to_whatsapp", new=AsyncMock(return_value=True)), \
                 patch.object(payment_routes, "generate_invoice_pdf", return_value=b"%PDF"):
                r = TestClient(app).post("/api/payments/razorpay/webhook", json=payload)
        finally:
            app.dependency_overrides.clear()
        self.assertEqual(r.status_code, 200)
        self.assertEqual(wallet_service.get_balance(db, SENDER), 2000)
        tips = [t for t in texts if "Current balance" in (t or "")]
        self.assertEqual(len(tips), 1)
        self.assertIn("Payment of ₹500 received. Current balance: ₹2,000", tips[0])
        db.close()
        engine.dispose()


class ButtonsShowBalanceAtSendTimeTests(FundedSlotGateTestCase):
    def test_payment_during_precheck_is_reflected_in_buttons(self):
        _make_customer(self.session, balance=700)

        async def slow_precheck(*_a, **_k):
            # A Razorpay payment commits while the photo is being checked.
            other = sessionmaker(bind=self.engine)()
            wallet_service.credit_wallet(other, SENDER, 500)
            other.close()
            return MagicMock(approved=True)

        with patch.object(self.webhook_module, "check_image_quality", new=AsyncMock(side_effect=slow_precheck)):
            self.assertEqual(self.client.post("/api/meta/webhook", json=_image_payload()).status_code, 200)
        buttons = self.webhook_module.send_product_selection_buttons
        buttons.assert_awaited_once()
        self.assertEqual(buttons.await_args.kwargs["balance"], 1200)  # not the 700 read on arrival
        self.assertEqual(self._balance(), 1200)                       # upload never charged


class PrecheckModelTests(unittest.TestCase):
    def test_default_model_is_not_the_retired_one(self):
        from app.config import Settings

        self.assertNotEqual(Settings.model_fields["IMAGE_PREVALIDATION_MODEL"].default, "gemini-2.5-flash")


if __name__ == "__main__":
    unittest.main()
