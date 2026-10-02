"""Phase 2 / U1+U2 (MON-2): every wallet change has exactly one ledger row, written atomically.

Invariant asserted after every scenario: for every customer, SUM(ledger.amount) == wallet_balance.
Runs on SQLite and on PostgreSQL (the concurrency and CHECK tests are PostgreSQL-only).
"""

import threading
import unittest
from unittest.mock import AsyncMock, patch

from sqlalchemy import func, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import sessionmaker

import app.models  # noqa: F401 -- register every model with Base.metadata
from app.api.routes import meta_webhook
from app.database import Base
from app.models.audit_log import AuditLog  # noqa: F401
from app.models.customer import Customer
from app.models.wallet_transaction import (
    KIND_CREDIT_PAYMENT,
    KIND_DEBIT_ORDER,
    KIND_OPENING_BALANCE,
    KIND_REFUND_ORDER,
    WalletTransaction,
)
from app.models.whatsapp_ingestion import WhatsAppIngestion
from app.services import meta_whatsapp_service as mws
from app.services import wallet_service
from tests.db_support import make_engine, postgres, requires_postgres

SENDER = "919812345678"


def ledger_sum(db, customer_id):
    return int(db.query(func.coalesce(func.sum(WalletTransaction.amount), 0))
               .filter(WalletTransaction.customer_id == customer_id).scalar())


def assert_ledger_matches_balances(test, db):
    """The invariant: no customer's balance differs from the sum of their ledger."""
    db.expire_all()
    for customer in db.query(Customer).all():
        test.assertEqual(ledger_sum(db, customer.id), customer.wallet_balance,
                         f"ledger and balance disagree for {customer.whatsapp_id}")


class LedgerTestBase(unittest.TestCase):
    concurrent = False

    def setUp(self):
        self.engine = make_engine(concurrent=self.concurrent)
        Base.metadata.create_all(bind=self.engine)
        self.Session = sessionmaker(bind=self.engine)
        self.db = self.Session()
        self.addCleanup(self.engine.dispose)
        self.addCleanup(self.db.close)

    def make_customer(self, balance=1000, whatsapp_id=SENDER):
        customer = Customer(whatsapp_id=whatsapp_id, full_name="T", business_name="B", gst_number="N/A",
                            address="A", wallet_balance=balance, is_registered=True)
        self.db.add(customer)
        self.db.flush()
        if balance:
            wallet_service.record_ledger(self.db, customer_id=customer.id, kind=KIND_OPENING_BALANCE, amount=balance)
        self.db.commit()
        return customer

    def make_ingestion(self, status="awaiting_choice", message_id="wamid.ledger.1", **extra):
        ingestion = WhatsAppIngestion(external_user_id=SENDER, external_message_id=message_id,
                                      external_media_id="m", channel="whatsapp", status=status, **extra)
        self.db.add(ingestion)
        self.db.commit()
        return ingestion


class ChargeAndRefundLedgerTests(LedgerTestBase):
    def test_charge_writes_one_debit_row_and_keeps_the_invariant(self):
        customer = self.make_customer(1000)
        ingestion = self.make_ingestion()
        charged, balance = wallet_service.charge_customer_balance(self.db, customer, 500, ingestion_id=ingestion.id)
        self.assertEqual((charged, balance), (True, 500))
        rows = self.db.query(WalletTransaction).filter(WalletTransaction.kind == KIND_DEBIT_ORDER).all()
        self.assertEqual([(r.amount, r.balance_after, r.ingestion_id) for r in rows], [(-500, 500, ingestion.id)])
        assert_ledger_matches_balances(self, self.db)

    def test_declined_charge_writes_nothing(self):
        customer = self.make_customer(300)
        charged, _ = wallet_service.charge_customer_balance(self.db, customer, 500, ingestion_id="x")
        self.assertFalse(charged)
        self.assertEqual(self.db.query(WalletTransaction).filter(WalletTransaction.kind == KIND_DEBIT_ORDER).count(), 0)
        assert_ledger_matches_balances(self, self.db)

    def test_uncommitted_charge_rolled_back_leaves_no_debit_and_no_ledger_row(self):
        customer = self.make_customer(1000)
        ingestion = self.make_ingestion()
        charged, _ = wallet_service.charge_customer_balance(
            self.db, customer, 500, ingestion_id=ingestion.id, commit=False)
        self.assertTrue(charged)
        self.db.rollback()                                    # the crash before the caller's commit
        self.assertEqual(self.db.query(Customer.wallet_balance).scalar(), 1000)
        assert_ledger_matches_balances(self, self.db)

    def test_database_refuses_a_second_debit_for_the_same_order(self):
        customer = self.make_customer(1000)
        ingestion = self.make_ingestion()
        wallet_service.charge_customer_balance(self.db, customer, 100, ingestion_id=ingestion.id)
        again, _ = wallet_service.charge_customer_balance(self.db, customer, 100, ingestion_id=ingestion.id)
        self.assertFalse(again)                               # IntegrityError caught, rolled back
        self.db.expire_all()
        self.assertEqual(self.db.query(Customer.wallet_balance).scalar(), 900)
        assert_ledger_matches_balances(self, self.db)

    def test_refund_writes_one_ledger_row_and_repeats_are_no_ops(self):
        customer = self.make_customer(200)
        ingestion = self.make_ingestion(status="failed", amount_charged=500)
        for _ in range(3):
            mws._refund_failed_ingestion(self.db, ingestion)
        self.db.expire_all()
        rows = self.db.query(WalletTransaction).filter(WalletTransaction.kind == KIND_REFUND_ORDER).all()
        self.assertEqual([(r.amount, r.ingestion_id) for r in rows], [(500, ingestion.id)])
        self.assertEqual(self.db.get(Customer, customer.id).wallet_balance, 700)
        assert_ledger_matches_balances(self, self.db)

    def test_payment_credit_is_recorded_once_per_payment_id(self):
        customer = self.make_customer(0)
        first = wallet_service.credit_wallet(self.db, SENDER, 500, ref="pay_1")
        second = wallet_service.credit_wallet(self.db, SENDER, 500, ref="pay_1")     # duplicate id
        self.assertEqual(first, 500)
        self.assertEqual(second, 0)                            # refused: must not look like a successful credit
        self.db.expire_all()
        self.assertEqual(self.db.get(Customer, customer.id).wallet_balance, 500)
        self.assertEqual(self.db.query(WalletTransaction).filter(WalletTransaction.kind == KIND_CREDIT_PAYMENT).count(), 1)
        assert_ledger_matches_balances(self, self.db)

    def test_ledger_rows_cannot_exceed_database_rules(self):
        customer = self.make_customer(100)
        self.db.add(WalletTransaction(customer_id=customer.id, kind="debit_order", amount=0, balance_after=100))
        with self.assertRaises(IntegrityError):
            self.db.flush()                                    # amount <> 0 CHECK
        self.db.rollback()


class ProductChoiceAtomicityTests(LedgerTestBase):
    """The WhatsApp tap -> debit -> queued path (meta_webhook._handle_product_choice)."""

    def _choose(self, ingestion_id):
        with patch.object(meta_webhook, "send_whatsapp_text", new=AsyncMock()), \
             patch.object(meta_webhook, "try_send_native_recharge", new=AsyncMock(return_value=True)):
            import asyncio
            return asyncio.run(meta_webhook._handle_product_choice(self.db, SENDER, "pack", ingestion_id))

    def test_paid_choice_debits_records_and_queues_in_one_step(self):
        customer = self.make_customer(1000)
        ingestion = self.make_ingestion()
        job = self._choose(ingestion.id)
        self.assertIsNotNone(job)
        self.db.expire_all()
        row = self.db.get(WhatsAppIngestion, ingestion.id)
        price = row.amount_charged
        self.assertGreater(price, 0)
        self.assertEqual(row.status, "pack_queued")
        self.assertEqual(self.db.get(Customer, customer.id).wallet_balance, 1000 - price)
        self.assertEqual(self.db.query(WalletTransaction).filter(
            WalletTransaction.kind == KIND_DEBIT_ORDER, WalletTransaction.ingestion_id == ingestion.id).count(), 1)
        assert_ledger_matches_balances(self, self.db)

    def test_crash_before_the_final_commit_takes_no_money_and_frees_the_photo(self):
        customer = self.make_customer(1000)
        ingestion = self.make_ingestion()
        real_commit, calls = self.db.commit, {"n": 0}

        def flaky_commit():
            calls["n"] += 1
            if calls["n"] == 2:                               # 1 = claim, 2 = debit + status
                raise RuntimeError("simulated crash between debit and status")
            return real_commit()

        with patch.object(self.db, "commit", side_effect=flaky_commit):
            with self.assertRaises(RuntimeError):
                self._choose(ingestion.id)
        self.db.expire_all()
        self.assertEqual(self.db.get(Customer, customer.id).wallet_balance, 1000)
        self.assertEqual(self.db.query(WalletTransaction).filter(WalletTransaction.kind == KIND_DEBIT_ORDER).count(), 0)
        self.assertEqual(self.db.get(WhatsAppIngestion, ingestion.id).status, "awaiting_choice")
        assert_ledger_matches_balances(self, self.db)

    def test_commit_error_after_the_server_committed_completes_the_order_from_the_ledger(self):
        customer = self.make_customer(1000)
        ingestion = self.make_ingestion()
        real_commit, calls = self.db.commit, {"n": 0}

        def commit_then_error():
            calls["n"] += 1
            real_commit()
            if calls["n"] == 2:                               # the commit DID reach the server, then the link dropped
                raise RuntimeError("connection dropped at COMMIT")

        with patch.object(self.db, "commit", side_effect=commit_then_error):
            job = self._choose(ingestion.id)
        self.assertIsNotNone(job)                              # the order proceeds; no false "insufficient balance"
        self.db.expire_all()
        row = self.db.get(WhatsAppIngestion, ingestion.id)
        self.assertEqual(row.status, "pack_queued")
        self.assertGreater(row.amount_charged, 0)
        self.assertEqual(self.db.query(WalletTransaction).filter(WalletTransaction.kind == KIND_DEBIT_ORDER).count(), 1)
        assert_ledger_matches_balances(self, self.db)

    def test_insufficient_balance_releases_the_claim_and_moves_no_money(self):
        customer = self.make_customer(100)
        ingestion = self.make_ingestion()
        self.assertIsNone(self._choose(ingestion.id))
        self.db.expire_all()
        self.assertEqual(self.db.get(Customer, customer.id).wallet_balance, 100)
        self.assertEqual(self.db.get(WhatsAppIngestion, ingestion.id).status, "awaiting_choice")
        assert_ledger_matches_balances(self, self.db)


@postgres
@requires_postgres
class LedgerUnderRealLocksTests(LedgerTestBase):
    concurrent = True

    def test_100_concurrent_debits_keep_ledger_equal_to_balance(self):
        customer = self.make_customer(1000)
        results, errors = [], []
        barrier = threading.Barrier(100, timeout=120)

        def debit(i):
            try:
                barrier.wait()
                with self.Session() as db:
                    c = db.get(Customer, customer.id)
                    ok, _ = wallet_service.charge_customer_balance(db, c, 50, ingestion_id=f"order-{i}")
                    results.append(ok)
            except Exception as exc:
                errors.append(repr(exc))

        threads = [threading.Thread(target=debit, args=(i,)) for i in range(100)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=180)
        self.assertEqual(errors, [])
        self.assertEqual(sum(results), 20)
        with self.Session() as db:
            self.assertEqual(db.query(WalletTransaction).filter(WalletTransaction.kind == KIND_DEBIT_ORDER).count(), 20)
            assert_ledger_matches_balances(self, db)
            self.assertEqual(db.get(Customer, customer.id).wallet_balance, 0)

    def test_database_rejects_a_negative_balance(self):
        customer = self.make_customer(100)
        with self.assertRaises(IntegrityError):
            with self.engine.begin() as conn:
                conn.execute(text("UPDATE customers SET wallet_balance = -1 WHERE id = :i"), {"i": customer.id})


if __name__ == "__main__":
    unittest.main()
