"""PostgreSQL-only behaviour that SQLite cannot show.

These run only with ``MORAA_TEST_DB=postgres`` (see tests/db_support.py). They turn
assumptions in the production-readiness assessment into measured facts:

* the guarded wallet debit never overspends under real row locks;
* the partial unique index behind every "money moves once" claim makes a second
  claimant WAIT for the first transaction and then fail (or succeed if the first
  rolled back), which is the READ COMMITTED behaviour the refund and payment code
  comments rely on;
* the startup check for that index works on PostgreSQL;
* foreign keys are enforced (SQLite does not enforce them in the test engines).
"""

import threading
import time
import unittest
from datetime import datetime, timezone
from unittest.mock import patch

from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import sessionmaker

import app.models  # noqa: F401 -- register every model with Base.metadata
from app import database
from app.database import Base
from app.models.audit_log import AuditLog
from app.models.customer import Customer
from app.models.processing_log import ProcessingLog
from app.services import wallet_service
from tests.db_support import postgres, postgres_engine, requires_postgres

CAPTURED = "razorpay_payment_captured"


def _new_schema():
    engine = postgres_engine()
    Base.metadata.create_all(bind=engine)
    return engine, sessionmaker(bind=engine)


@postgres
@requires_postgres
class WalletDebitUnderRealLocksTests(unittest.TestCase):
    def setUp(self):
        self.engine, self.Session = _new_schema()
        with self.Session() as db:
            customer = Customer(whatsapp_id="919800000001", full_name="T", business_name="B",
                                gst_number="N/A", address="A", wallet_balance=1000, is_registered=True)
            db.add(customer)
            db.commit()
            self.customer_id = customer.id

    def tearDown(self):
        self.engine.dispose()

    def test_100_concurrent_debits_never_overspend(self):
        price, attempts = 50, 100
        results, barrier = [], threading.Barrier(attempts, timeout=30)

        def debit():
            barrier.wait()                      # release all 100 together, BEFORE taking a pooled connection
            with self.Session() as db:
                customer = db.get(Customer, self.customer_id)
                charged, _ = wallet_service.charge_customer_balance(db, customer, price)
                results.append(charged)

        threads = [threading.Thread(target=debit) for _ in range(attempts)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=60)

        with self.Session() as db:
            balance = db.get(Customer, self.customer_id).wallet_balance
        self.assertEqual(len(results), attempts)
        self.assertEqual(sum(results), 1000 // price)      # exactly the funded debits succeed
        self.assertEqual(balance, 0)                        # all money accounted for, never negative


@postgres
@requires_postgres
class UniqueClaimSemanticsTests(unittest.TestCase):
    """The claim-first pattern depends on how a second INSERT behaves while the first is open."""

    def setUp(self):
        self.engine, self.Session = _new_schema()

    def tearDown(self):
        self.engine.dispose()

    def _claim(self, db, resource_id):
        db.add(AuditLog(action=CAPTURED, resource_id=resource_id, resource_type="razorpay_payment", status="success"))
        db.flush()

    def _wait_until_a_session_is_blocked_on_a_lock(self, timeout=20.0):
        """Poll pg_stat_activity until some backend is genuinely waiting for a lock."""
        deadline = time.monotonic() + timeout
        with self.engine.connect() as conn:
            while time.monotonic() < deadline:
                waiting = conn.execute(text(
                    "SELECT count(*) FROM pg_stat_activity "
                    "WHERE datname = current_database() AND wait_event_type = 'Lock'"
                )).scalar()
                if waiting:
                    return
                time.sleep(0.05)
        self.fail("no session ever blocked on the unique-index lock")

    def _second_claimant(self, resource_id, outcome):
        started = time.monotonic()
        with self.Session() as db:
            try:
                self._claim(db, resource_id)
                db.commit()
                outcome["result"] = "claimed"
            except IntegrityError:
                db.rollback()
                outcome["result"] = "duplicate"
        outcome["seconds"] = time.monotonic() - started

    def test_second_claimant_waits_then_fails_when_first_commits(self):
        outcome = {}
        with self.Session() as first:
            self._claim(first, "pay_wait_commit")                  # first claim flushed, NOT committed
            second = threading.Thread(target=self._second_claimant, args=("pay_wait_commit", outcome))
            second.start()
            self._wait_until_a_session_is_blocked_on_a_lock()      # proven: it is waiting on the first claim
            self.assertTrue(second.is_alive())
            first.commit()
            second.join(timeout=20)
        self.assertEqual(outcome.get("result"), "duplicate")
        with self.Session() as db:
            self.assertEqual(db.query(AuditLog).filter(AuditLog.resource_id == "pay_wait_commit").count(), 1)

    def test_second_claimant_succeeds_when_first_rolls_back(self):
        outcome = {}
        with self.Session() as first:
            self._claim(first, "pay_wait_rollback")
            second = threading.Thread(target=self._second_claimant, args=("pay_wait_rollback", outcome))
            second.start()
            self._wait_until_a_session_is_blocked_on_a_lock()
            self.assertTrue(second.is_alive())
            first.rollback()                                       # first transaction aborts (e.g. credit failed)
            second.join(timeout=20)
        self.assertEqual(outcome.get("result"), "claimed")
        with self.Session() as db:
            self.assertEqual(db.query(AuditLog).filter(AuditLog.resource_id == "pay_wait_rollback").count(), 1)

    def test_20_racing_claimants_produce_exactly_one_winner(self):
        winners, barrier = [], threading.Barrier(20, timeout=30)

        def race():
            barrier.wait()
            with self.Session() as db:
                try:
                    self._claim(db, "pay_race")
                    db.commit()
                    winners.append(1)
                except IntegrityError:
                    db.rollback()

        threads = [threading.Thread(target=race) for _ in range(20)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=60)
        self.assertEqual(len(winners), 1)

    def test_unrelated_actions_are_not_blocked_by_the_partial_index(self):
        with self.Session() as db:
            for _ in range(3):
                db.add(AuditLog(action="razorpay_payment_credit_failed", resource_id="pay_repeat", status="pending"))
            db.commit()
            self.assertEqual(db.query(AuditLog).filter(AuditLog.resource_id == "pay_repeat").count(), 3)


@postgres
@requires_postgres
class StartupIndexCheckTests(unittest.TestCase):
    def setUp(self):
        self.engine, _ = _new_schema()

    def tearDown(self):
        self.engine.dispose()

    def test_money_once_index_detected_and_missing_index_reported(self):
        with patch.object(database, "engine", self.engine):
            self.assertIs(database.money_once_index_present(), True)
            with self.engine.begin() as conn:
                conn.execute(text(f"DROP INDEX {database.MONEY_ONCE_INDEX}"))
            self.assertIs(database.money_once_index_present(), False)


@postgres
@requires_postgres
class ForeignKeyEnforcementTests(unittest.TestCase):
    """SQLite test engines do not enforce foreign keys; PostgreSQL does (cf. DATA-05)."""

    def setUp(self):
        self.engine, self.Session = _new_schema()

    def tearDown(self):
        self.engine.dispose()

    def test_processing_log_for_unknown_request_is_rejected(self):
        with self.Session() as db:
            db.add(ProcessingLog(request_id="no-such-request", processing_step="upload", status="started",
                                 started_at=datetime.now(timezone.utc)))
            with self.assertRaises(IntegrityError):
                db.commit()


if __name__ == "__main__":
    unittest.main()
