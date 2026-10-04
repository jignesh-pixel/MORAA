"""Phase 2 / MON-11: a paid Razorpay payment with no usable phone is parked for review (never credited to a
made-up wallet), and a payment that belongs to a WhatsApp Pay order is left to the WhatsApp Pay path."""

import json
import threading
import unittest
from unittest.mock import AsyncMock, patch

from sqlalchemy.orm import sessionmaker

import app.models  # noqa: F401 -- register every model with Base.metadata
from app.config import settings
from app.database import Base, get_db
from app.models.audit_log import AuditLog
from app.models.customer import Customer
from app.models.pending_payment import PendingPayment
from app.models.wallet_transaction import WalletTransaction
from app.models.whatsapp_payment_order import WhatsAppPaymentOrder
from app.services import pending_payment_service as pps
from tests.db_support import make_engine, postgres, requires_postgres
from tests.test_razorpay_clawback import PAYMENT, _payment_event
from tests.test_wallet_ledger import SENDER, LedgerTestBase, assert_ledger_matches_balances


def _no_phone_event(payment_id=PAYMENT, customer_id=None, amount=50000):
    entity = {"id": payment_id, "amount": amount, "currency": "INR", "status": "captured"}
    if customer_id:
        entity["customer_id"] = customer_id
    return {"event": "payment.captured", "payload": {"payment": {"entity": entity}}}


class _RouteBase(unittest.TestCase):
    def setUp(self):
        from fastapi.testclient import TestClient

        from app.api.routes import payment_routes
        from app.main import app

        self.app = app
        self.engine = make_engine()
        Base.metadata.create_all(bind=self.engine)
        self.session = sessionmaker(bind=self.engine)()
        def _override_get_db():
            yield self.session

        app.dependency_overrides[get_db] = _override_get_db
        self.client = TestClient(app)
        patches = [
            patch.object(settings, "RAZORPAY_WEBHOOK_SECRET", ""),
            patch.object(payment_routes, "send_whatsapp_text", new=AsyncMock(return_value=True)),
            patch.object(payment_routes, "send_document_to_whatsapp", new=AsyncMock(return_value=True)),
            patch.object(payment_routes, "generate_invoice_pdf", return_value=b"%PDF"),
            patch.object(payment_routes, "dispatch_payment_invoice", new=AsyncMock()),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)
        # Cleanups run last-in first-out: close the session before disposing the engine.
        self.addCleanup(self.engine.dispose)
        self.addCleanup(self.session.close)
        self.addCleanup(app.dependency_overrides.clear)

    def post(self, payload):
        response = self.client.post("/api/payments/razorpay/webhook", content=json.dumps(payload).encode(),
                                    headers={"Content-Type": "application/json"})
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()["status"]

    def customers(self):
        self.session.expire_all()
        return self.session.query(Customer).all()

    def pending(self):
        self.session.expire_all()
        return self.session.query(PendingPayment).all()


class NoPhoneTests(_RouteBase):
    def test_payment_without_a_phone_is_parked_not_credited(self):
        self.assertEqual(self.post(_no_phone_event()), "missing_phone")
        self.assertEqual(self.customers(), [])
        rows = self.pending()
        self.assertEqual([(r.payment_id, r.amount_rupees, r.status, r.reason) for r in rows],
                         [(PAYMENT, 500, "pending", "missing_phone")])

    def test_repeated_events_park_it_once(self):
        self.post(_no_phone_event())
        self.post(_no_phone_event())
        self.assertEqual(len(self.pending()), 1)

    def test_a_razorpay_customer_id_is_not_turned_into_a_wallet(self):
        self.assertEqual(self.post(_no_phone_event(customer_id="cust_Q123456789")), "missing_phone")
        self.assertEqual(self.customers(), [])           # before: a customer row with whatsapp_id "cust_..."
        row = self.pending()[0]
        self.assertEqual((row.reason, row.payer_hint), ("not_a_phone_number", "cust_Q123456789"))

    def test_a_later_event_that_carries_the_phone_credits_it_and_closes_the_parked_row(self):
        self.post(_no_phone_event())
        self.assertEqual(self.post(_payment_event()), "ok")
        self.assertEqual([c.wallet_balance for c in self.customers()], [500])
        row = self.pending()[0]
        self.assertEqual((row.status, row.credited_whatsapp_id), ("credited", SENDER))


class WhatsAppPayOverlapTests(_RouteBase):
    def make_order(self, status="sent", credited=False, pg_payment_id=None, pg_order_id=None):
        self.session.add(WhatsAppPaymentOrder(
            reference_id="ref-1", whatsapp_id=SENDER, amount_rupees=500, total_paise=50000, status=status,
            credited=credited, pg_payment_id=pg_payment_id, pg_order_id=pg_order_id))
        self.session.commit()

    def event_with_order(self, order_id="order_Q1"):
        event = _payment_event()
        event["payload"]["payment"]["entity"]["order_id"] = order_id
        return event

    def setUp(self):
        super().setUp()
        for p in (patch.object(settings, "WHATSAPP_PAY_ENABLED", True),
                  patch.object(settings, "WHATSAPP_PAY_RECONCILE_INTERVAL_SECONDS", 300)):
            p.start()
            self.addCleanup(p.stop)

    def test_payment_linked_by_payment_id_is_left_to_the_whatsapp_pay_path(self):
        self.make_order(pg_payment_id=PAYMENT)
        self.assertEqual(self.post(_payment_event()), "whatsapp_pay_order")
        self.assertEqual(self.customers(), [])

    def test_payment_linked_by_razorpay_order_id_is_left_to_the_whatsapp_pay_path(self):
        self.make_order(pg_order_id="order_Q1")
        self.assertEqual(self.post(self.event_with_order()), "whatsapp_pay_order")
        self.assertEqual(self.customers(), [])

    def test_an_order_the_sweep_never_rechecks_is_parked_not_skipped(self):
        for state in ("created", "dispatch_failed"):
            self.session.query(WhatsAppPaymentOrder).delete()
            self.session.query(PendingPayment).delete()
            self.session.commit()
            self.make_order(status=state, pg_payment_id=PAYMENT)
            self.assertEqual(self.post(_payment_event()), "whatsapp_pay_order_review", state)
            self.assertEqual(self.customers(), [])

    def test_when_whatsapp_pay_is_off_the_payment_is_credited_here(self):
        self.make_order(pg_payment_id=PAYMENT)
        with patch.object(settings, "WHATSAPP_PAY_ENABLED", False):
            self.assertEqual(self.post(_payment_event()), "ok")
        self.assertEqual([c.wallet_balance for c in self.customers()], [500])

    def test_an_already_credited_payment_is_not_parked_again(self):
        self.post(_payment_event())                                             # credited normally first
        self.make_order(status="amount_mismatch", pg_payment_id=PAYMENT)
        self.assertEqual(self.post(_payment_event()), "already_processed")
        self.assertEqual(self.pending(), [])

    def test_an_unlinked_payment_is_credited_normally(self):
        self.make_order(pg_payment_id="pay_other")
        self.assertEqual(self.post(self.event_with_order("order_other")), "ok")
        self.assertEqual([c.wallet_balance for c in self.customers()], [500])

    def test_a_linked_order_that_will_not_be_credited_is_parked_for_review(self):
        self.make_order(status="amount_mismatch", pg_payment_id=PAYMENT)
        self.assertEqual(self.post(_payment_event()), "whatsapp_pay_order_review")
        self.assertEqual(self.customers(), [])
        self.assertEqual([(r.reason, r.status) for r in self.pending()], [("whatsapp_pay_not_credited", "pending")])


class ManualCreditTests(LedgerTestBase):
    def park(self, payment_id=PAYMENT, amount=500, currency="INR"):
        self.db.add(PendingPayment(payment_id=payment_id, amount_rupees=amount, currency=currency, reason="missing_phone"))
        self.db.commit()

    def test_parked_payment_is_credited_once_with_a_ledger_row(self):
        customer = self.make_customer(100)
        self.park()
        result = pps.credit_pending_payment(self.db, PAYMENT, "+91 98123 45678")
        self.assertEqual(result["status"], "credited")
        self.db.expire_all()
        self.assertEqual(self.db.get(Customer, customer.id).wallet_balance, 600)
        self.assertEqual(pps.credit_pending_payment(self.db, PAYMENT, SENDER)["status"], "already_credited")
        self.db.expire_all()
        self.assertEqual(self.db.get(Customer, customer.id).wallet_balance, 600)
        self.assertEqual(self.db.query(WalletTransaction).filter(WalletTransaction.ref == PAYMENT).count(), 1)
        self.assertEqual(self.db.query(AuditLog).filter(AuditLog.resource_id == PAYMENT,
                                                         AuditLog.action == "razorpay_payment_captured").count(), 1)
        assert_ledger_matches_balances(self, self.db)

    def test_unknown_payment_unknown_customer_and_foreign_currency_change_nothing(self):
        customer = self.make_customer(100)
        self.assertEqual(pps.credit_pending_payment(self.db, "pay_nope", SENDER)["status"], "unknown_payment")
        self.park()
        self.assertEqual(pps.credit_pending_payment(self.db, PAYMENT, "447700900123")["status"], "unknown_customer")
        self.park("pay_usd", currency="USD")
        self.assertEqual(pps.credit_pending_payment(self.db, "pay_usd", SENDER)["status"], "not_creditable")
        self.db.expire_all()
        self.assertEqual(self.db.get(Customer, customer.id).wallet_balance, 100)
        assert_ledger_matches_balances(self, self.db)

    def test_a_payment_already_credited_by_the_webhook_cannot_be_credited_again_by_hand(self):
        customer = self.make_customer(0)
        self.park()
        self.db.add(AuditLog(user_id=None, action="razorpay_payment_captured", resource_id=PAYMENT,
                             resource_type="razorpay_payment", status="success"))
        self.db.commit()
        self.assertEqual(pps.credit_pending_payment(self.db, PAYMENT, SENDER)["status"], "already_credited")
        self.db.expire_all()
        self.assertEqual(self.db.get(Customer, customer.id).wallet_balance, 0)


@postgres
@requires_postgres
class ManualCreditUnderRealLocksTests(LedgerTestBase):
    concurrent = True

    def test_ten_people_crediting_the_same_parked_payment_credit_it_once(self):
        customer = self.make_customer(0)
        self.db.add(PendingPayment(payment_id=PAYMENT, amount_rupees=500, currency="INR", reason="missing_phone"))
        self.db.commit()
        results, errors = [], []
        barrier = threading.Barrier(10, timeout=120)

        def credit():
            try:
                barrier.wait()
                with self.Session() as db:
                    results.append(pps.credit_pending_payment(db, PAYMENT, SENDER)["status"])
            except Exception as exc:
                errors.append(repr(exc))

        threads = [threading.Thread(target=credit) for _ in range(10)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=180)
        self.assertEqual(errors, [])
        self.assertEqual(results.count("credited"), 1)
        with self.Session() as db:
            self.assertEqual(db.get(Customer, customer.id).wallet_balance, 500)
            assert_ledger_matches_balances(self, db)


if __name__ == "__main__":
    unittest.main()
