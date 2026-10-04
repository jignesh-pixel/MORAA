"""Phase 2 / U3: refunds follow the ledger, recovery finds held money, retries cannot give free orders.

Each scenario ends by asserting the ledger invariant (SUM(ledger) == wallet_balance)."""

import asyncio
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock, patch

from app.api.routes import meta_webhook
from app.models.customer import Customer
from app.models.wallet_transaction import KIND_DEBIT_ORDER, KIND_REFUND_ORDER, WalletTransaction
from app.models.whatsapp_ingestion import PRODUCT_PACK_1, WhatsAppIngestion
from app.services import meta_whatsapp_service as mws
from app.services import wallet_service
from tests.test_wallet_ledger import SENDER, LedgerTestBase, assert_ledger_matches_balances

OLD = timedelta(hours=1)


class RecoveryBase(LedgerTestBase):
    def setUp(self):
        super().setUp()
        for p in (
            patch("app.database.SessionLocal", return_value=self.db),
            patch.object(self.db, "close"),
            patch.object(mws, "send_whatsapp_text", new=AsyncMock(return_value=True)),
        ):
            p.start()
            self.addCleanup(p.stop)

    def age(self, ingestion, by=OLD):
        self.db.query(WhatsAppIngestion).filter(WhatsAppIngestion.id == ingestion.id).update(
            {WhatsAppIngestion.updated_at: datetime.now(timezone.utc) - by}, synchronize_session=False)
        self.db.commit()
        self.db.expire_all()

    def paid_order(self, customer, status, price=500, product=PRODUCT_PACK_1, message_id="wamid.paid.1"):
        """An order exactly as the product-choice step leaves it: debit + ledger + amount_charged."""
        ingestion = self.make_ingestion(status=status, message_id=message_id, product_code=product)
        charged, _ = wallet_service.charge_customer_balance(self.db, customer, price, ingestion_id=ingestion.id)
        assert charged
        ingestion.amount_charged = price
        self.db.commit()
        return ingestion


class RefundFollowsTheLedgerTests(RecoveryBase):
    def test_refund_amount_and_wallet_come_from_the_debit_row(self):
        customer = self.make_customer(1000)
        ingestion = self.paid_order(customer, "failed", price=500)
        ingestion.amount_charged = 99999          # a corrupted/edited column must not change what is refunded
        self.db.commit()
        mws._refund_failed_ingestion(self.db, ingestion)
        self.db.expire_all()
        self.assertEqual(self.db.get(Customer, customer.id).wallet_balance, 1000)
        refund = self.db.query(WalletTransaction).filter(WalletTransaction.kind == KIND_REFUND_ORDER).one()
        self.assertEqual(refund.amount, 500)
        assert_ledger_matches_balances(self, self.db)

    def test_order_with_no_charge_recorded_creates_no_money(self):
        customer = self.make_customer(200)
        ingestion = self.make_ingestion(status="failed")        # amount_charged NULL, no debit row
        mws._refund_failed_ingestion(self.db, ingestion)
        self.db.expire_all()
        self.assertEqual(self.db.get(Customer, customer.id).wallet_balance, 200)
        self.assertEqual(self.db.query(WalletTransaction).filter(WalletTransaction.kind == KIND_REFUND_ORDER).count(), 0)
        assert_ledger_matches_balances(self, self.db)


class RecoverySweepTests(RecoveryBase):
    def test_failed_order_that_kept_the_money_is_refunded_once(self):
        customer = self.make_customer(1000)
        ingestion = self.paid_order(customer, "failed", price=500)      # crashed between status and refund
        self.age(ingestion)
        first = asyncio.run(mws.recover_unrefunded_failed_orders(timedelta(minutes=2)))
        second = asyncio.run(mws.recover_unrefunded_failed_orders(timedelta(minutes=2)))
        self.assertEqual((first, second), (1, 0))
        self.db.expire_all()
        self.assertEqual(self.db.get(Customer, customer.id).wallet_balance, 1000)
        assert_ledger_matches_balances(self, self.db)

    def test_a_just_failed_order_is_left_for_its_own_refund(self):
        customer = self.make_customer(1000)
        self.paid_order(customer, "failed", price=500)                   # updated_at = now
        self.assertEqual(asyncio.run(mws.recover_unrefunded_failed_orders(timedelta(minutes=2))), 0)

    def test_abandoned_claim_without_a_debit_is_released(self):
        self.make_customer(1000)
        ingestion = self.make_ingestion(status="choice_claimed", product_code=PRODUCT_PACK_1)
        self.age(ingestion)
        self.assertEqual(asyncio.run(mws.release_abandoned_choice_claims(timedelta(minutes=5))), 1)
        self.db.expire_all()
        row = self.db.get(WhatsAppIngestion, ingestion.id)
        self.assertEqual((row.status, row.product_code), ("awaiting_choice", None))

    def test_a_claim_that_has_a_debit_is_never_released(self):
        customer = self.make_customer(1000)
        ingestion = self.paid_order(customer, "choice_claimed")
        self.age(ingestion)
        self.assertEqual(asyncio.run(mws.release_abandoned_choice_claims(timedelta(minutes=5))), 0)
        assert_ledger_matches_balances(self, self.db)

    def test_a_photo_cut_off_while_processing_is_closed_and_the_customer_asked_to_resend(self):
        customer = self.make_customer(1000)
        ingestion = self.make_ingestion(status="received")
        self.age(ingestion)
        self.assertEqual(asyncio.run(mws.recover_interrupted_photos(timedelta(minutes=10))), 1)
        self.assertEqual(asyncio.run(mws.recover_interrupted_photos(timedelta(minutes=10))), 0)
        self.db.expire_all()
        self.assertEqual(self.db.get(WhatsAppIngestion, ingestion.id).status, "rejected")
        self.assertEqual(self.db.get(Customer, customer.id).wallet_balance, 1000)
        self.assertIn("nothing was charged", mws.send_whatsapp_text.await_args.args[1])

    def test_a_photo_still_being_processed_is_left_alone(self):
        self.make_customer(1000)
        self.make_ingestion(status="received")                          # just arrived
        self.assertEqual(asyncio.run(mws.recover_interrupted_photos(timedelta(minutes=10))), 0)

    def test_a_photo_cut_off_while_processing_is_closed_and_the_customer_asked_to_resend(self):
        customer = self.make_customer(1000)
        ingestion = self.make_ingestion(status="received")
        self.age(ingestion)
        self.assertEqual(asyncio.run(mws.recover_interrupted_photos(timedelta(minutes=10))), 1)
        self.assertEqual(asyncio.run(mws.recover_interrupted_photos(timedelta(minutes=10))), 0)
        self.db.expire_all()
        self.assertEqual(self.db.get(WhatsAppIngestion, ingestion.id).status, "rejected")
        self.assertEqual(self.db.get(Customer, customer.id).wallet_balance, 1000)
        self.assertIn("nothing was charged", mws.send_whatsapp_text.await_args.args[1])

    def test_a_photo_still_being_processed_is_left_alone(self):
        self.make_customer(1000)
        self.make_ingestion(status="received")                          # just arrived
        self.assertEqual(asyncio.run(mws.recover_interrupted_photos(timedelta(minutes=10))), 0)

    def test_stuck_paid_order_is_failed_and_refunded_exactly_once(self):
        customer = self.make_customer(1000)
        ingestion = self.paid_order(customer, "pack_queued", price=500)
        self.age(ingestion)
        self.assertEqual(asyncio.run(mws.recover_stuck_paid_orders(timedelta(minutes=10))), 1)
        self.assertEqual(asyncio.run(mws.recover_stuck_paid_orders(timedelta(minutes=10))), 0)
        self.db.expire_all()
        self.assertEqual(self.db.get(Customer, customer.id).wallet_balance, 1000)
        assert_ledger_matches_balances(self, self.db)


class WorkerCannotDeliverASweptOrderTests(RecoveryBase):
    def test_status_advance_is_refused_once_recovery_has_failed_the_order(self):
        customer = self.make_customer(1000)
        ingestion = self.paid_order(customer, "processing")
        self.db.query(WhatsAppIngestion).filter(WhatsAppIngestion.id == ingestion.id).update(
            {WhatsAppIngestion.status: "failed"}, synchronize_session=False)       # the sweep got there first
        self.db.commit()
        self.assertFalse(mws._advance_status(self.db, ingestion.id, "processing", "generated"))
        self.db.expire_all()
        self.assertEqual(self.db.get(WhatsAppIngestion, ingestion.id).status, "failed")

    def test_status_advance_succeeds_when_nothing_interfered(self):
        customer = self.make_customer(1000)
        ingestion = self.paid_order(customer, "processing")
        self.assertTrue(mws._advance_status(self.db, ingestion.id, "processing", "generated"))


class RetryCannotGiveAFreeOrderTests(RecoveryBase):
    def _retry(self, ingestion_id):
        tasks = MagicMock()
        # retry_delivery is a plain function now (FastAPI runs it on a worker thread), not a coroutine
        return meta_webhook.retry_delivery(ingestion_id, tasks, current_user=object(), db=self.db), tasks

    def test_retry_of_a_refunded_order_is_refused(self):
        customer = self.make_customer(1000)
        ingestion = self.paid_order(customer, "failed", price=500)
        mws._refund_failed_ingestion(self.db, ingestion)
        result, tasks = self._retry(ingestion.id)
        self.assertEqual(result["status"], "error")
        tasks.add_task.assert_not_called()

    def test_retry_of_an_order_that_never_had_a_product_is_refused(self):
        self.make_customer(1000)
        ingestion = self.make_ingestion(status="failed")
        result, tasks = self._retry(ingestion.id)
        self.assertEqual(result["status"], "error")
        tasks.add_task.assert_not_called()

    def test_retry_of_a_still_paid_failed_order_is_queued_once(self):
        customer = self.make_customer(1000)
        ingestion = self.paid_order(customer, "failed", price=500)      # refund never happened: money still held
        result, tasks = self._retry(ingestion.id)
        self.assertEqual(result["status"], "queued")
        tasks.add_task.assert_called_once()
        again, tasks2 = self._retry(ingestion.id)                       # now 'stored': a second retry is not allowed
        self.assertEqual(again["status"], "error")
        tasks2.add_task.assert_not_called()
        self.assertEqual(self.db.query(WalletTransaction).filter(WalletTransaction.kind == KIND_DEBIT_ORDER).count(), 1)


if __name__ == "__main__":
    unittest.main()
