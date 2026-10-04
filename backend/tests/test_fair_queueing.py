"""Q-6: global provider concurrency with priority, and a per-customer limit on orders in progress."""

import asyncio
import unittest
from unittest.mock import AsyncMock, patch

from app.ai.concurrency_gate import PRIORITY_PACK, PRIORITY_SINGLE, PriorityGate
from app.api.routes import meta_webhook
from app.config import settings
from app.models.customer import Customer
from app.models.wallet_transaction import WalletTransaction
from app.models.whatsapp_ingestion import WhatsAppIngestion
from app.services import meta_whatsapp_service as mws
from tests.test_wallet_ledger import SENDER, LedgerTestBase, assert_ledger_matches_balances


class GateTests(unittest.TestCase):
    def test_no_limit_means_no_waiting(self):
        async def run():
            gate = PriorityGate()
            async with gate.slot(PRIORITY_PACK):
                async with gate.slot(PRIORITY_PACK):
                    return gate.active

        with patch.object(settings, "MAX_CONCURRENT_PROVIDER_CALLS", 0):
            self.assertEqual(asyncio.run(run()), 0)

    def test_never_more_than_the_limit_run_at_once(self):
        peak = 0
        running = 0

        async def call(gate):
            nonlocal peak, running
            async with gate.slot(PRIORITY_PACK):
                running += 1
                peak = max(peak, running)
                await asyncio.sleep(0.01)
                running -= 1

        async def run():
            gate = PriorityGate()
            await asyncio.gather(*[call(gate) for _ in range(12)])
            return gate

        with patch.object(settings, "MAX_CONCURRENT_PROVIDER_CALLS", 3):
            gate = asyncio.run(run())
        self.assertEqual(peak, 3)
        self.assertEqual((gate.active, gate.waiting), (0, 0))

    def test_a_single_shot_jumps_ahead_of_waiting_pack_calls(self):
        order = []

        async def call(gate, name, priority, hold=0.0):
            async with gate.slot(priority):
                order.append(name)
                await asyncio.sleep(hold)

        async def run():
            gate = PriorityGate()
            first = asyncio.create_task(call(gate, "pack-1", PRIORITY_PACK, hold=0.05))     # takes the only slot
            await asyncio.sleep(0.01)
            tasks = [asyncio.create_task(call(gate, f"pack-{i}", PRIORITY_PACK)) for i in (2, 3, 4)]
            await asyncio.sleep(0.01)
            tasks.append(asyncio.create_task(call(gate, "single", PRIORITY_SINGLE)))          # arrives last
            await asyncio.gather(first, *tasks)

        with patch.object(settings, "MAX_CONCURRENT_PROVIDER_CALLS", 1):
            asyncio.run(run())
        self.assertEqual(order, ["pack-1", "single", "pack-2", "pack-3", "pack-4"])

    def test_a_cancelled_waiter_does_not_leak_a_slot(self):
        async def run():
            gate = PriorityGate()
            holder = asyncio.create_task(self._hold(gate, 0.05))
            await asyncio.sleep(0.01)
            waiter = asyncio.create_task(self._hold(gate, 0.0))
            await asyncio.sleep(0.01)
            waiter.cancel()
            await asyncio.gather(holder, waiter, return_exceptions=True)
            return gate

        with patch.object(settings, "MAX_CONCURRENT_PROVIDER_CALLS", 1):
            gate = asyncio.run(run())
        self.assertEqual((gate.active, gate.waiting), (0, 0))

    @staticmethod
    async def _hold(gate, seconds):
        async with gate.slot(PRIORITY_PACK):
            await asyncio.sleep(seconds)


class PerCustomerCapTests(LedgerTestBase):
    def setUp(self):
        super().setUp()
        self.sent = AsyncMock()
        for p in (patch.object(meta_webhook, "send_whatsapp_text", new=self.sent),
                  patch.object(meta_webhook, "try_send_native_recharge", new=AsyncMock(return_value=True)),
                  patch.object(mws, "DRY_RUN_IMAGE_MODE", False),
                  patch.object(settings, "MAX_GENERATIONS_PER_DAY", 1000)):
            p.start()
            self.addCleanup(p.stop)

    def choose(self, ingestion_id):
        return asyncio.run(meta_webhook._handle_product_choice(self.db, SENDER, "pack", ingestion_id))

    def busy(self, n):
        for i in range(n):
            self.make_ingestion(status="processing", message_id=f"wamid.busy.{i}")

    def test_a_customer_at_the_limit_is_declined_with_nothing_charged_and_the_photo_stays_choosable(self):
        customer = self.make_customer(1000)
        self.busy(3)
        ingestion = self.make_ingestion()
        with patch.object(settings, "MAX_INFLIGHT_ORDERS_PER_CUSTOMER", 3):
            self.assertIsNone(self.choose(ingestion.id))
        self.db.expire_all()
        self.assertEqual(self.db.get(Customer, customer.id).wallet_balance, 1000)
        row = self.db.get(WhatsAppIngestion, ingestion.id)
        self.assertEqual((row.status, row.product_code), ("awaiting_choice", None))
        self.assertEqual(self.db.query(WalletTransaction).filter(WalletTransaction.kind == "debit_order").count(), 0)
        self.assertIn("nothing was charged", self.sent.await_args.args[1])
        assert_ledger_matches_balances(self, self.db)

    def test_below_the_limit_the_order_is_charged_as_before(self):
        self.make_customer(1000)
        self.busy(2)
        ingestion = self.make_ingestion()
        with patch.object(settings, "MAX_INFLIGHT_ORDERS_PER_CUSTOMER", 3):
            self.assertIsNotNone(self.choose(ingestion.id))

    def test_zero_turns_the_limit_off(self):
        self.make_customer(1000)
        self.busy(10)
        ingestion = self.make_ingestion()
        with patch.object(settings, "MAX_INFLIGHT_ORDERS_PER_CUSTOMER", 0):
            self.assertIsNotNone(self.choose(ingestion.id))

    def test_team_orders_are_exempt(self):
        customer = self.make_customer(0)
        customer.tier = "ADMIN"
        self.db.commit()
        self.busy(5)
        ingestion = self.make_ingestion()
        with patch.object(settings, "MAX_INFLIGHT_ORDERS_PER_CUSTOMER", 3):
            self.assertIsNotNone(self.choose(ingestion.id))


if __name__ == "__main__":
    unittest.main()
