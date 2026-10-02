"""Phase 2 / MON-4: Razorpay payments must be INR; refunds and lost disputes take the money back.

Policy under test (owner decision): take what is in the wallet, never go below zero, and flag the
part that was already spent for manual review. Invariant after every scenario:
SUM(ledger.amount) == wallet_balance for every customer.
"""

import json
import threading
import unittest
from unittest.mock import AsyncMock, patch

from sqlalchemy.orm import sessionmaker

import app.models  # noqa: F401 -- register every model with Base.metadata
from app.config import settings
from app.database import get_db
from app.models.audit_log import AuditLog
from app.models.customer import Customer
from app.models.wallet_transaction import (
    KIND_CREDIT_PAYMENT,
    KIND_DEBIT_DISPUTE,
    KIND_DEBIT_REFUND,
    WalletTransaction,
)
from app.services import wallet_service
from tests.db_support import make_engine, postgres, requires_postgres
from tests.test_wallet_ledger import SENDER, LedgerTestBase, assert_ledger_matches_balances

PAYMENT = "pay_Qclaw0001"


def _payment_event(payment_id=PAYMENT, amount=50000, currency="INR", event="payment.captured"):
    entity = {"id": payment_id, "amount": amount, "status": "captured", "notes": {"sender_id": SENDER}}
    if currency is not None:
        entity["currency"] = currency
    return {"event": event, "payload": {"payment": {"entity": entity}}}


def _refund_event(refund_id="rfnd_1", payment_id=PAYMENT, amount=50000, currency="INR", event="refund.processed"):
    return {"event": event, "payload": {"refund": {"entity": {
        "id": refund_id, "payment_id": payment_id, "amount": amount, "currency": currency}}}}


def _dispute_event(dispute_id="disp_1", payment_id=PAYMENT, amount=50000, event="payment.dispute.lost"):
    return {"event": event, "payload": {"dispute": {"entity": {
        "id": dispute_id, "payment_id": payment_id, "amount": amount, "currency": "INR"}}}}


class ClawbackServiceTests(LedgerTestBase):
    def credit(self, amount=500, payment_id=PAYMENT, balance=0):
        customer = self.make_customer(balance)
        self.assertEqual(
            wallet_service.credit_wallet(self.db, SENDER, amount, ref=payment_id, kind=KIND_CREDIT_PAYMENT),
            balance + amount,
        )
        return customer

    def claw(self, entity_id="rfnd_1", amount=500, kind=KIND_DEBIT_REFUND, payment_id=PAYMENT):
        return wallet_service.claw_back_payment(
            self.db, payment_id=payment_id, entity_id=entity_id, amount=amount, kind=kind)

    def balance(self, customer):
        self.db.expire_all()
        return self.db.get(Customer, customer.id).wallet_balance

    def test_full_refund_takes_the_money_back(self):
        customer = self.credit(500)
        result = self.claw(amount=500)
        self.assertEqual(result, {"outcome": "applied", "taken": 500, "shortfall": 0})
        self.assertEqual(self.balance(customer), 0)
        row = self.db.query(WalletTransaction).filter(WalletTransaction.kind == KIND_DEBIT_REFUND).one()
        self.assertEqual((row.amount, row.balance_after, row.ref), (-500, 0, f"{PAYMENT}:rfnd_1"))
        audit = self.db.query(AuditLog).filter(AuditLog.action == "razorpay_clawback").one()
        self.assertEqual(audit.status, "success")
        assert_ledger_matches_balances(self, self.db)

    def test_partly_spent_wallet_takes_what_is_there_and_flags_the_rest(self):
        customer = self.credit(500)
        wallet_service.charge_customer_balance(self.db, customer, 400, ingestion_id="order-spent")
        result = self.claw(amount=500)
        self.assertEqual(result, {"outcome": "applied", "taken": 100, "shortfall": 400})
        self.assertEqual(self.balance(customer), 0)
        audit = self.db.query(AuditLog).filter(AuditLog.action == "razorpay_clawback").one()
        self.assertEqual(audit.status, "pending")
        self.assertEqual(json.loads(audit.details)["shortfall"], 400)
        assert_ledger_matches_balances(self, self.db)

    def test_empty_wallet_takes_nothing_but_still_records_and_flags(self):
        customer = self.credit(500)
        wallet_service.charge_customer_balance(self.db, customer, 500, ingestion_id="order-spent")
        result = self.claw(amount=500)
        self.assertEqual(result, {"outcome": "applied", "taken": 0, "shortfall": 500})
        self.assertEqual(self.db.query(WalletTransaction).filter(WalletTransaction.kind == KIND_DEBIT_REFUND).count(), 0)
        # A later top-up must not be touched when Razorpay repeats the same event.
        wallet_service.credit_wallet(self.db, SENDER, 300, ref="pay_Qother", kind=KIND_CREDIT_PAYMENT)
        self.assertEqual(self.claw(amount=500)["outcome"], "duplicate")
        self.assertEqual(self.balance(customer), 300)
        assert_ledger_matches_balances(self, self.db)

    def test_same_refund_twice_is_applied_once(self):
        customer = self.credit(500)
        self.assertEqual(self.claw(amount=200)["outcome"], "applied")
        self.assertEqual(self.claw(amount=200)["outcome"], "duplicate")
        self.assertEqual(self.balance(customer), 300)
        assert_ledger_matches_balances(self, self.db)

    def test_refunds_and_a_dispute_never_take_more_than_the_payment_credited(self):
        customer = self.credit(500, balance=1000)
        self.claw("rfnd_1", 300)
        self.claw("rfnd_2", 150)
        result = self.claw("disp_1", 500, kind=KIND_DEBIT_DISPUTE)     # only 50 of the 500 is left to take back
        self.assertEqual(result["taken"], 50)
        self.assertEqual(self.balance(customer), 1000)                  # exactly the pre-payment balance
        audit = self.db.query(AuditLog).filter(AuditLog.resource_id == "disp_1").one()
        self.assertEqual(audit.status, "pending")                       # asked for more than was ever credited
        assert_ledger_matches_balances(self, self.db)

    def test_whatsapp_pay_credit_can_be_taken_back_too(self):
        customer = self.make_customer(0)
        wallet_service.credit_wallet(self.db, SENDER, 500, ref=PAYMENT, kind="credit_whatsapp_pay")
        self.assertEqual(self.claw(amount=500)["taken"], 500)
        self.assertEqual(self.balance(customer), 0)
        assert_ledger_matches_balances(self, self.db)

    def test_unknown_payment_changes_nothing(self):
        customer = self.credit(500)
        self.assertEqual(self.claw(payment_id="pay_never_credited")["outcome"], "no_payment")
        self.assertEqual(self.balance(customer), 500)
        assert_ledger_matches_balances(self, self.db)


@postgres
@requires_postgres
class ClawbackUnderRealLocksTests(LedgerTestBase):
    concurrent = True

    def test_same_refund_delivered_20_times_at_once_is_applied_once(self):
        customer = self.make_customer(0)
        wallet_service.credit_wallet(self.db, SENDER, 500, ref=PAYMENT, kind=KIND_CREDIT_PAYMENT)
        results, errors = [], []
        barrier = threading.Barrier(20, timeout=120)

        def deliver():
            try:
                barrier.wait()
                with self.Session() as db:
                    results.append(wallet_service.claw_back_payment(
                        db, payment_id=PAYMENT, entity_id="rfnd_1", amount=200, kind=KIND_DEBIT_REFUND)["outcome"])
            except Exception as exc:
                errors.append(repr(exc))

        threads = [threading.Thread(target=deliver) for _ in range(20)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=180)
        self.assertEqual(errors, [])
        self.assertEqual(results.count("applied"), 1)
        self.assertEqual(results.count("duplicate"), 19)
        with self.Session() as db:
            self.assertEqual(db.get(Customer, customer.id).wallet_balance, 300)
            assert_ledger_matches_balances(self, db)

    def test_refund_racing_with_spending_never_makes_the_balance_negative(self):
        customer = self.make_customer(0)
        wallet_service.credit_wallet(self.db, SENDER, 500, ref=PAYMENT, kind=KIND_CREDIT_PAYMENT)
        errors = []
        barrier = threading.Barrier(11, timeout=120)

        def spend(i):
            try:
                barrier.wait()
                with self.Session() as db:
                    wallet_service.charge_customer_balance(db, db.get(Customer, customer.id), 50, ingestion_id=f"o{i}")
            except Exception as exc:
                errors.append(repr(exc))

        def refund():
            try:
                barrier.wait()
                with self.Session() as db:
                    wallet_service.claw_back_payment(
                        db, payment_id=PAYMENT, entity_id="rfnd_1", amount=500, kind=KIND_DEBIT_REFUND)
            except Exception as exc:
                errors.append(repr(exc))

        threads = [threading.Thread(target=spend, args=(i,)) for i in range(10)] + [threading.Thread(target=refund)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=180)
        self.assertEqual(errors, [])
        with self.Session() as db:
            self.assertGreaterEqual(db.get(Customer, customer.id).wallet_balance, 0)
            assert_ledger_matches_balances(self, db)


class RazorpayRefundRouteTests(unittest.TestCase):
    def setUp(self):
        from fastapi.testclient import TestClient

        from app.api.routes import payment_routes
        from app.main import app

        self.app = app
        self.engine = make_engine()
        app.dependency_overrides.clear()
        from app.database import Base
        Base.metadata.create_all(bind=self.engine)
        self.session = sessionmaker(bind=self.engine)()

        def _override_get_db():
            yield self.session

        app.dependency_overrides[get_db] = _override_get_db
        self.client = TestClient(app)
        self.patches = [
            patch.object(settings, "RAZORPAY_WEBHOOK_SECRET", ""),
            patch.object(payment_routes, "send_whatsapp_text", new=AsyncMock(return_value=True)),
            patch.object(payment_routes, "send_document_to_whatsapp", new=AsyncMock(return_value=True)),
            patch.object(payment_routes, "generate_invoice_pdf", return_value=b"%PDF"),
            patch.object(payment_routes, "dispatch_payment_invoice", new=AsyncMock()),
        ]
        for p in self.patches:
            p.start()
        self.addCleanup(self.cleanup)

    def cleanup(self):
        for p in reversed(self.patches):
            p.stop()
        self.app.dependency_overrides.clear()
        self.session.close()
        self.engine.dispose()

    def post(self, payload):
        response = self.client.post("/api/payments/razorpay/webhook", content=json.dumps(payload).encode(),
                                    headers={"Content-Type": "application/json"})
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()["status"]

    def balance(self):
        self.session.expire_all()
        customer = self.session.query(Customer).filter(Customer.whatsapp_id == SENDER).first()
        return customer.wallet_balance if customer else None

    def reviews(self):
        self.session.expire_all()
        return [json.loads(r.details)["reason"] for r in
                self.session.query(AuditLog).filter(AuditLog.action == "razorpay_payment_review").all()]

    # -- currency ------------------------------------------------------------------------------

    def test_inr_payment_is_credited(self):
        self.assertEqual(self.post(_payment_event()), "ok")
        self.assertEqual(self.balance(), 500)

    def test_non_inr_payment_is_not_credited_and_is_flagged_once(self):
        self.assertEqual(self.post(_payment_event(currency="USD")), "unsupported_currency")
        self.assertEqual(self.post(_payment_event(currency="USD")), "unsupported_currency")
        self.assertIsNone(self.balance())
        self.assertEqual(self.reviews(), ["currency_not_inr"])

    def test_payment_without_a_currency_is_not_credited(self):
        self.assertEqual(self.post(_payment_event(currency=None)), "unsupported_currency")
        self.assertIsNone(self.balance())

    def test_lowercase_inr_is_accepted(self):
        self.assertEqual(self.post(_payment_event(currency="inr")), "ok")
        self.assertEqual(self.balance(), 500)

    # -- refunds and disputes ----------------------------------------------------------------------

    def test_processed_refund_takes_the_money_back_once(self):
        self.post(_payment_event())
        self.assertEqual(self.post(_refund_event(amount=20000)), "clawed_back")
        self.assertEqual(self.post(_refund_event(amount=20000)), "already_processed")
        self.assertEqual(self.balance(), 300)
        assert_ledger_matches_balances(self, self.session)

    def test_refund_larger_than_balance_is_partial_and_flagged(self):
        self.post(_payment_event())
        customer = self.session.query(Customer).filter(Customer.whatsapp_id == SENDER).one()
        wallet_service.charge_customer_balance(self.session, customer, 400, ingestion_id="spent")
        self.assertEqual(self.post(_refund_event(amount=50000)), "clawed_back_partial")
        self.assertEqual(self.balance(), 0)
        assert_ledger_matches_balances(self, self.session)

    def test_only_processed_refunds_and_lost_disputes_move_money(self):
        self.post(_payment_event())
        self.assertEqual(self.post(_refund_event(event="refund.created")), "ignored")
        self.assertEqual(self.post(_refund_event(event="refund.failed")), "ignored")
        self.assertEqual(self.post(_dispute_event(event="payment.dispute.won")), "ignored")
        self.assertEqual(self.balance(), 500)

    def test_dispute_opened_is_flagged_without_moving_money(self):
        self.post(_payment_event())
        self.assertEqual(self.post(_dispute_event(event="payment.dispute.created")), "dispute_flagged")
        self.assertEqual(self.balance(), 500)
        self.assertEqual(self.reviews(), ["dispute_opened"])

    def test_dispute_lost_takes_the_money_back(self):
        self.post(_payment_event())
        self.assertEqual(self.post(_dispute_event()), "clawed_back")
        self.assertEqual(self.balance(), 0)
        kinds = [r.kind for r in self.session.query(WalletTransaction).all()]
        self.assertIn(KIND_DEBIT_DISPUTE, kinds)

    def test_old_refund_for_a_payment_we_never_credited_is_flagged(self):
        self.post(_payment_event())
        old = _refund_event(payment_id="pay_unknown")
        old["payload"]["refund"]["entity"]["created_at"] = 1_600_000_000
        self.assertEqual(self.post(old), "no_matching_credit")
        self.assertEqual(self.balance(), 500)
        self.assertEqual(self.reviews(), ["no_matching_credit"])

    def test_refund_arriving_before_its_payment_is_retried_then_applied(self):
        """The payment event is still in flight: answer 503 so Razorpay redelivers the refund."""
        early = self.client.post("/api/payments/razorpay/webhook", content=json.dumps(_refund_event(amount=20000)).encode(),
                                 headers={"Content-Type": "application/json"})
        self.assertEqual(early.status_code, 503)
        self.assertIsNone(self.balance())
        self.post(_payment_event())                                           # the payment now credits
        self.assertEqual(self.post(_refund_event(amount=20000)), "clawed_back")   # Razorpay's retry
        self.assertEqual(self.balance(), 300)
        assert_ledger_matches_balances(self, self.session)

    def test_refund_under_half_a_rupee_is_ignored_without_a_false_alarm(self):
        self.post(_payment_event())
        self.assertEqual(self.post(_refund_event(amount=40)), "ignored_sub_rupee")
        self.assertEqual(self.balance(), 500)
        self.assertEqual(self.reviews(), [])

    def test_dispute_flag_is_closed_when_the_dispute_is_lost(self):
        self.post(_payment_event())
        self.post(_dispute_event(event="payment.dispute.created"))
        self.post(_dispute_event())
        self.session.expire_all()
        row = self.session.query(AuditLog).filter(AuditLog.action == "razorpay_payment_review").one()
        self.assertEqual(row.status, "resolved")

    def test_refund_without_a_currency_is_judged_by_the_verified_payment(self):
        self.post(_payment_event())
        refund = _refund_event(amount=20000)
        del refund["payload"]["refund"]["entity"]["currency"]
        self.assertEqual(self.post(refund), "clawed_back")
        self.assertEqual(self.balance(), 300)

    def test_refund_in_another_currency_is_not_applied(self):
        self.post(_payment_event())
        self.assertEqual(self.post(_refund_event(currency="USD")), "unsupported_currency")
        self.assertEqual(self.balance(), 500)

    def test_malformed_refund_is_acknowledged_without_error(self):
        self.assertEqual(self.post({"event": "refund.processed", "payload": {}}), "unparseable")
        self.assertEqual(self.post(_refund_event(amount=0)), "unparseable")


if __name__ == "__main__":
    unittest.main()
