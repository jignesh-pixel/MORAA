"""Phase 2 / UX-4: an order that cannot be generated today (daily cap reached, kill switch off) is declined
BEFORE the customer is charged, and the photo stays choosable."""

import asyncio
import unittest
from unittest.mock import AsyncMock, patch

from app.ai import image_generation_manager as igm
from app.api.routes import meta_webhook
from app.config import settings
from app.models.customer import Customer
from app.models.wallet_transaction import WalletTransaction
from app.models.whatsapp_ingestion import WhatsAppIngestion
from app.services import meta_whatsapp_service as mws
from tests.test_wallet_ledger import SENDER, LedgerTestBase, assert_ledger_matches_balances


class CapacityPeekTests(unittest.TestCase):
    def setUp(self):
        self._saved = (igm._spend_day, igm._spend_count)
        igm._spend_day, igm._spend_count = None, 0
        self.addCleanup(lambda: setattr(igm, "_spend_day", self._saved[0]))
        self.addCleanup(lambda: setattr(igm, "_spend_count", self._saved[1]))

    def test_peek_never_consumes_a_slot(self):
        with patch.object(settings, "MAX_GENERATIONS_PER_DAY", 6), patch.object(settings, "GENERATION_ENABLED", True):
            for _ in range(20):
                self.assertIsNone(igm.generation_capacity_blocked(6))
            self.assertIsNone(igm.reserve_generation_slots(6))            # the real reservation still has all 6
            self.assertIsNotNone(igm.generation_capacity_blocked(1))      # and now none are left
            self.assertIsNotNone(igm.reserve_generation_slots(1))

    def test_kill_switch_blocks(self):
        with patch.object(settings, "GENERATION_ENABLED", False):
            self.assertIn("disabled", igm.generation_capacity_blocked(1))


class OrderDeclinedBeforeChargeTests(LedgerTestBase):
    def setUp(self):
        super().setUp()
        self.sent = AsyncMock()
        for p in (patch.object(meta_webhook, "send_whatsapp_text", new=self.sent),
                  patch.object(meta_webhook, "try_send_native_recharge", new=AsyncMock(return_value=True)),
                  patch.object(mws, "DRY_RUN_IMAGE_MODE", False)):
            p.start()
            self.addCleanup(p.stop)
        self._saved = (igm._spend_day, igm._spend_count)
        igm._spend_day, igm._spend_count = None, 0
        self.addCleanup(lambda: setattr(igm, "_spend_day", self._saved[0]))
        self.addCleanup(lambda: setattr(igm, "_spend_count", self._saved[1]))

    def choose(self, ingestion_id, button="pack"):
        return asyncio.run(meta_webhook._handle_product_choice(self.db, SENDER, button, ingestion_id))

    def assert_declined_untouched(self, customer, ingestion, balance):
        self.db.expire_all()
        self.assertEqual(self.db.get(Customer, customer.id).wallet_balance, balance)
        row = self.db.get(WhatsAppIngestion, ingestion.id)
        self.assertEqual((row.status, row.product_code, row.amount_charged), ("awaiting_choice", None, None))
        self.assertEqual(self.db.query(WalletTransaction).filter(WalletTransaction.kind == "debit_order").count(), 0)
        self.assertIn("nothing was charged", self.sent.await_args.args[1])
        assert_ledger_matches_balances(self, self.db)

    def test_pack_is_declined_when_the_daily_cap_leaves_no_room_for_a_whole_pack(self):
        customer = self.make_customer(1000)
        ingestion = self.make_ingestion()
        # One call per style: a ceiling one short of the whole pack (7 of 8) must decline it, never a partial pack.
        with patch.object(settings, "MAX_GENERATIONS_PER_DAY", mws.pack_generation_count() - 1):
            self.assertIsNone(self.choose(ingestion.id))
        self.assert_declined_untouched(customer, ingestion, 1000)

    def test_pack_is_declined_when_todays_ceiling_is_already_used_up(self):
        customer = self.make_customer(1000)
        ingestion = self.make_ingestion()
        cap = mws.pack_generation_count()
        with patch.object(settings, "MAX_GENERATIONS_PER_DAY", cap):
            self.assertIsNone(igm.reserve_generation_slots(cap))         # today's ceiling already used up
            self.assertIsNone(self.choose(ingestion.id))
        self.assert_declined_untouched(customer, ingestion, 1000)

    def test_order_is_declined_when_generation_is_switched_off(self):
        customer = self.make_customer(1000)
        ingestion = self.make_ingestion()
        with patch.object(settings, "GENERATION_ENABLED", False):
            self.assertIsNone(self.choose(ingestion.id))
        self.assert_declined_untouched(customer, ingestion, 1000)

    def test_declined_photo_can_be_chosen_again_once_there_is_room(self):
        customer = self.make_customer(1000)
        ingestion = self.make_ingestion()
        with patch.object(settings, "GENERATION_ENABLED", False):
            self.assertIsNone(self.choose(ingestion.id))
        self.assertIsNotNone(self.choose(ingestion.id))                     # room again: charged and queued
        self.db.expire_all()
        self.assertLess(self.db.get(Customer, customer.id).wallet_balance, 1000)

    def test_admin_orders_are_not_blocked_by_the_daily_cap(self):
        customer = self.make_customer(0)
        customer.tier = "ADMIN"
        self.db.commit()
        ingestion = self.make_ingestion()
        with patch.object(settings, "MAX_GENERATIONS_PER_DAY", 1):
            self.assertIsNotNone(self.choose(ingestion.id))

    def test_an_order_that_fits_is_charged_as_before(self):
        customer = self.make_customer(1000)
        ingestion = self.make_ingestion()
        with patch.object(settings, "MAX_GENERATIONS_PER_DAY", 1000):
            self.assertIsNotNone(self.choose(ingestion.id))
        self.db.expire_all()
        self.assertEqual(self.db.get(WhatsAppIngestion, ingestion.id).status, "pack_queued")


if __name__ == "__main__":
    unittest.main()
