"""Phase 2 / MON-9: two quick taps must never both get the last complimentary trial credit."""

import asyncio
import threading
import time
from unittest.mock import AsyncMock, patch

from app.api.routes import meta_webhook
from app.models.whatsapp_ingestion import WhatsAppIngestion
from tests.db_support import postgres, requires_postgres
from tests.test_wallet_ledger import SENDER, LedgerTestBase


class _TrialBase(LedgerTestBase):
    def setUp(self):
        super().setUp()
        # Patched once for the whole test (never inside worker threads: threads restoring each
        # other's saved originals would leave a mock behind for every later test).
        for patcher in (
            patch.object(meta_webhook, "send_whatsapp_text", new=AsyncMock()),
            patch.object(meta_webhook, "try_send_native_recharge", new=AsyncMock(return_value=True)),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)

    def make_trial_customer(self, total=1, used=0):
        customer = self.make_customer(0)
        customer.tier = "TRIAL"
        customer.trial_credits_total = total
        customer.trial_credits_used = used
        self.db.commit()
        return customer

    def tap(self, session, ingestion_id):
        return asyncio.run(meta_webhook._handle_product_choice(session, SENDER, "pack", ingestion_id))

    def status_of(self, ingestion_id):
        self.db.expire_all()
        return self.db.get(WhatsAppIngestion, ingestion_id).status


class TrialCreditSequentialTests(_TrialBase):
    def test_second_tap_is_not_given_the_last_credit(self):
        self.make_trial_customer(total=1)
        a = self.make_ingestion(message_id="wamid.trial.a")
        b = self.make_ingestion(message_id="wamid.trial.b")
        self.assertIsNotNone(self.tap(self.db, a.id))
        self.assertIsNone(self.tap(self.db, b.id))                 # no credit left and no money: held
        self.assertEqual(self.status_of(a.id), "pack_queued")
        self.assertEqual(self.status_of(b.id), "awaiting_choice")

    def test_delivered_order_keeps_its_credit_reserved_until_it_is_counted(self):
        """Between 'delivered' and the credit being recorded, another tap must not get the same credit."""
        self.make_trial_customer(total=1)
        a = self.make_ingestion(message_id="wamid.trial.a")
        b = self.make_ingestion(message_id="wamid.trial.b")
        self.assertIsNotNone(self.tap(self.db, a.id))
        self.db.query(WhatsAppIngestion).filter(WhatsAppIngestion.id == a.id).update({"status": "delivered"})
        self.db.commit()                                         # delivered, credit not counted yet
        self.assertIsNone(self.tap(self.db, b.id))
        self.assertEqual(self.status_of(b.id), "awaiting_choice")

    def test_an_old_delivered_order_that_was_never_metered_does_not_hold_a_credit_forever(self):
        from datetime import datetime, timedelta, timezone

        self.make_trial_customer(total=1)
        old = self.make_ingestion(message_id="wamid.trial.old", status="delivered", amount_charged=0)
        self.db.query(WhatsAppIngestion).filter(WhatsAppIngestion.id == old.id).update(
            {"updated_at": datetime.now(timezone.utc) - timedelta(hours=2)})
        self.db.commit()
        fresh = self.make_ingestion(message_id="wamid.trial.fresh")
        self.assertIsNotNone(self.tap(self.db, fresh.id))

    def test_two_credits_cover_two_taps(self):
        self.make_trial_customer(total=2)
        a = self.make_ingestion(message_id="wamid.trial.a")
        b = self.make_ingestion(message_id="wamid.trial.b")
        self.assertIsNotNone(self.tap(self.db, a.id))
        self.assertIsNotNone(self.tap(self.db, b.id))


@postgres
@requires_postgres
class TrialCreditUnderRealLocksTests(_TrialBase):
    concurrent = True

    def test_ten_simultaneous_taps_on_the_last_credit_queue_exactly_one(self):
        self.make_trial_customer(total=1)
        ids = [self.make_ingestion(message_id=f"wamid.trial.{i}").id for i in range(10)]

        # Widen the gap between "is a credit free?" and "save the order", so a missing lock cannot hide.
        real_check = meta_webhook.ent.trial_credits_available

        def slow_check(*args, **kwargs):
            result = real_check(*args, **kwargs)
            time.sleep(0.3)
            return result

        patcher = patch.object(meta_webhook.ent, "trial_credits_available", new=slow_check)
        patcher.start()
        self.addCleanup(patcher.stop)
        results, errors = [], []
        barrier = threading.Barrier(len(ids), timeout=120)

        def run(ingestion_id):
            try:
                barrier.wait()
                with self.Session() as session:
                    results.append(self.tap(session, ingestion_id) is not None)
            except Exception as exc:
                errors.append(repr(exc))

        threads = [threading.Thread(target=run, args=(i,)) for i in ids]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=180)
        self.assertEqual(errors, [])
        self.assertEqual(sum(results), 1)
        self.db.expire_all()
        queued = self.db.query(WhatsAppIngestion).filter(WhatsAppIngestion.status == "pack_queued").count()
        self.assertEqual(queued, 1)
