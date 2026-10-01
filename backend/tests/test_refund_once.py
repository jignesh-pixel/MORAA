"""Phase 0 / U7 (MON-1): a failed order is refunded exactly once, even under a race.

Before the fix the wallet was credited and committed before the audit claim,
so N racing refunds could credit N times (measured: 20 refunds of 500 credited
8,500-10,000). Now the claim is flushed first and the partial unique index
uq_audit_logs_money_once lets only one transaction credit.
"""

import os
import tempfile
import threading
import unittest
from unittest.mock import patch

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import app.models  # noqa: F401 -- register every model with Base.metadata
from app.database import Base
from app.models.audit_log import AuditLog
from app.models.customer import Customer
from app.models.whatsapp_ingestion import WhatsAppIngestion
from app.services import meta_whatsapp_service as mws
from app.services import wallet_service

SENDER = "919812345678"
START_BALANCE = 200
CHARGED = 500


class RefundOnceTests(unittest.TestCase):
    def setUp(self):
        fd, self.db_path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        # A real file so every thread gets its own connection and transaction.
        self.engine = create_engine(
            f"sqlite:///{self.db_path}",
            connect_args={"check_same_thread": False, "timeout": 15},
        )
        Base.metadata.create_all(bind=self.engine)
        self.Session = sessionmaker(bind=self.engine)
        with self.Session() as db:
            db.add(Customer(
                whatsapp_id=SENDER, full_name="Test Jeweller", business_name="Test Gems",
                gst_number="N/A", address="Surat", wallet_balance=START_BALANCE,
                is_registered=True,
            ))
            ingestion = WhatsAppIngestion(
                external_user_id=SENDER, external_message_id="wamid.refund.once",
                external_media_id="m1", channel="whatsapp", status="failed",
                amount_charged=CHARGED,
            )
            db.add(ingestion)
            db.commit()
            self.ingestion_id = ingestion.id

    def tearDown(self):
        self.engine.dispose()
        os.remove(self.db_path)

    def _balance(self):
        with self.Session() as db:
            return db.query(Customer.wallet_balance).filter(Customer.whatsapp_id == SENDER).scalar()

    def _refund_rows(self):
        with self.Session() as db:
            return db.query(AuditLog).filter(
                AuditLog.action == mws.REFUND_AUDIT_ACTION,
                AuditLog.resource_id == self.ingestion_id,
            ).count()

    def _refund_in_new_session(self):
        with self.Session() as db:
            ingestion = db.get(WhatsAppIngestion, self.ingestion_id)
            mws._refund_failed_ingestion(db, ingestion)

    def test_single_refund_credits_and_records_once(self):
        self._refund_in_new_session()
        self.assertEqual(self._balance(), START_BALANCE + CHARGED)
        self.assertEqual(self._refund_rows(), 1)

    def test_repeat_refund_is_a_no_op(self):
        for _ in range(3):
            self._refund_in_new_session()
        self.assertEqual(self._balance(), START_BALANCE + CHARGED)
        self.assertEqual(self._refund_rows(), 1)

    def test_racing_refunds_credit_exactly_once(self):
        racers = 12
        barrier = threading.Barrier(racers, timeout=20)
        real_lookup = wallet_service.find_customer_by_phone

        def lookup_after_everyone_passed_the_fast_check(db, phone):
            # Every racer has already seen "no refund row yet"; now all of
            # them try to claim at once -- the worst case for the old code.
            barrier.wait()
            return real_lookup(db, phone)

        errors = []

        def run():
            try:
                self._refund_in_new_session()
            except Exception as exc:  # the function must never raise
                errors.append(exc)

        with patch.object(wallet_service, "find_customer_by_phone",
                          side_effect=lookup_after_everyone_passed_the_fast_check):
            threads = [threading.Thread(target=run) for _ in range(racers)]
            for t in threads:
                t.start()
            for t in threads:
                t.join(timeout=60)

        self.assertEqual(errors, [])
        self.assertEqual(self._balance(), START_BALANCE + CHARGED)
        self.assertEqual(self._refund_rows(), 1)

    def test_unknown_customer_leaves_no_claim_and_no_credit(self):
        with self.Session() as db:
            db.query(Customer).filter(Customer.whatsapp_id == SENDER).update(
                {Customer.whatsapp_id: "910000000000"}, synchronize_session=False
            )
            db.commit()
        self._refund_in_new_session()
        self.assertEqual(self._refund_rows(), 0)

    def test_failed_credit_rolls_back_the_claim(self):
        with patch.object(wallet_service, "credit_wallet", return_value=0):
            self._refund_in_new_session()
        self.assertEqual(self._refund_rows(), 0)
        self.assertEqual(self._balance(), START_BALANCE)
        # A later attempt (e.g. startup recovery) can still refund.
        self._refund_in_new_session()
        self.assertEqual(self._balance(), START_BALANCE + CHARGED)
        self.assertEqual(self._refund_rows(), 1)


class MoneyOnceIndexCheckTests(unittest.TestCase):
    def test_index_detected_on_the_test_database(self):
        from app.database import Base as AppBase, engine, money_once_index_present

        AppBase.metadata.create_all(bind=engine)
        self.assertIs(money_once_index_present(), True)


if __name__ == "__main__":
    unittest.main()
