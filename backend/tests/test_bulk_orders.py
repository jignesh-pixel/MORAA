"""UX-1: a burst of photos becomes one bulk order the customer confirms once."""

import asyncio
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock, patch

from app.api.routes import meta_webhook
from app.config import settings
from app.models.customer import Customer
from app.models.wallet_transaction import KIND_DEBIT_ORDER, WalletTransaction
from app.models.whatsapp_ingestion import WhatsAppIngestion
from app.services import bulk_orders
from app.services import meta_whatsapp_service as mws
from tests.test_wallet_ledger import SENDER, LedgerTestBase, assert_ledger_matches_balances

PRICE = 50


class _Base(LedgerTestBase):
    def setUp(self):
        super().setUp()
        self.sent = AsyncMock()
        self.buttons = AsyncMock(return_value=True)
        patches = [
            patch.object(meta_webhook, "send_whatsapp_text", new=self.sent),
            patch.object(meta_webhook, "try_send_native_recharge", new=AsyncMock(return_value=True)),
            patch("app.services.meta_whatsapp_service.send_reply_buttons", new=self.buttons),
            patch.object(mws, "DRY_RUN_IMAGE_MODE", False),
            patch.object(settings, "WHITE_BG_PRICE_RUPEES", PRICE),
            patch.object(settings, "MAX_GENERATIONS_PER_DAY", 1000),
            patch.object(settings, "BULK_ENABLED", True),
            patch.object(settings, "OUTBOX_ENABLED", True),
            patch("app.database.SessionLocal", return_value=self.db),
            patch.object(self.db, "close"),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)

    def photos(self, n, age_seconds=60, status="awaiting_choice", start=0):
        rows = []
        for i in range(n):
            row = self.make_ingestion(status=status, message_id=f"wamid.bulk.{start + i}")
            row.created_at = datetime.now(timezone.utc) - timedelta(seconds=age_seconds)
            rows.append(row)
        self.db.commit()
        return rows

    def group(self, n=3):
        self.photos(n)
        group_id, count = bulk_orders.prepare_group(SENDER)
        self.assertEqual(count, n)
        return group_id

    def confirm(self, group_id, button=bulk_orders.BULK_OK):
        return asyncio.run(meta_webhook._handle_bulk_choice(self.db, SENDER, button, group_id))


class BurstDetectionTests(_Base):
    def test_a_second_photo_right_after_the_first_is_a_burst_but_the_first_alone_is_not(self):
        first, = self.photos(1, age_seconds=5)
        self.assertFalse(bulk_orders.is_burst(self.db, first))
        second = self.make_ingestion(message_id="wamid.bulk.second")
        self.assertTrue(bulk_orders.is_burst(self.db, second))

    def test_an_old_photo_is_not_part_of_the_burst(self):
        self.photos(1, age_seconds=600)
        newcomer = self.make_ingestion(message_id="wamid.bulk.new")
        self.assertFalse(bulk_orders.is_burst(self.db, newcomer))

    def test_buttons_and_enabled_switches(self):
        self.assertEqual(bulk_orders.parse_bulk_button_id(bulk_orders.bulk_button_id(bulk_orders.BULK_OK, "g1")),
                         (bulk_orders.BULK_OK, "g1"))
        self.assertIsNone(bulk_orders.parse_bulk_button_id("gv_white:abc"))
        with patch.object(settings, "OUTBOX_ENABLED", False):
            self.assertFalse(bulk_orders.is_enabled())


class GroupingTests(_Base):
    def test_waiting_photos_are_gathered_once(self):
        self.photos(4)
        first = bulk_orders.prepare_group(SENDER)
        self.assertEqual(first[1], 4)
        self.assertIsNone(bulk_orders.prepare_group(SENDER))              # already grouped: no second prompt

    def test_a_single_photo_or_a_customer_still_sending_is_not_prompted(self):
        self.photos(1)
        self.assertIsNone(bulk_orders.prepare_group(SENDER))
        self.photos(2, age_seconds=1, start=10)                          # the newest arrived a second ago
        self.assertIsNone(bulk_orders.prepare_group(SENDER))

    def test_the_cap_per_confirmation(self):
        self.photos(7)
        with patch.object(settings, "BULK_MAX_PHOTOS", 5):
            self.assertEqual(bulk_orders.prepare_group(SENDER)[1], 5)

    def test_the_prompt_states_the_count_total_and_balance(self):
        self.make_customer(1000)
        self.photos(12)
        self.assertTrue(asyncio.run(bulk_orders.send_group_prompt(SENDER)))
        body, buttons = self.buttons.await_args.args[1], self.buttons.await_args.args[2]
        self.assertIn("12 photos", body)
        self.assertIn("₹600", body)
        self.assertIn("₹1,000", body)
        self.assertEqual([b[1] for b in buttons], ["Confirm ₹600", "Cancel"])


class ConfirmTests(_Base):
    def test_confirming_charges_every_photo_and_queues_them_all(self):
        customer = self.make_customer(1000)
        gid = self.group(3)
        jobs = self.confirm(gid)
        self.assertEqual(len(jobs), 3)
        self.assertTrue(all(job[0] is meta_webhook.process_whatsapp_white_bg for job in jobs))
        self.db.expire_all()
        self.assertEqual(self.db.get(Customer, customer.id).wallet_balance, 1000 - 3 * PRICE)
        debits = self.db.query(WalletTransaction).filter(WalletTransaction.kind == KIND_DEBIT_ORDER).all()
        self.assertEqual((len(debits), {d.amount for d in debits}), (3, {-PRICE}))
        rows = self.db.query(WhatsAppIngestion).filter(WhatsAppIngestion.group_id == gid).all()
        self.assertEqual({(r.status, r.amount_charged, r.product_code) for r in rows}, {("white_queued", PRICE, "WHITE_BG")})
        assert_ledger_matches_balances(self, self.db)
        self.assertIn("3 Clean Studio Shots", self.sent.await_args.args[1])

    def test_not_enough_money_charges_nothing_and_keeps_the_photos(self):
        customer = self.make_customer(100)
        gid = self.group(3)
        self.assertEqual(self.confirm(gid), [])
        self.db.expire_all()
        self.assertEqual(self.db.get(Customer, customer.id).wallet_balance, 100)
        self.assertEqual(self.db.query(WalletTransaction).filter(WalletTransaction.kind == KIND_DEBIT_ORDER).count(), 0)
        rows = self.db.query(WhatsAppIngestion).filter(WhatsAppIngestion.group_id == gid).all()
        self.assertEqual({(r.status, r.product_code) for r in rows}, {("awaiting_choice", None)})

    def test_a_double_tap_charges_once(self):
        customer = self.make_customer(1000)
        gid = self.group(3)
        self.assertEqual(len(self.confirm(gid)), 3)
        self.assertEqual(self.confirm(gid), [])
        self.assertIn("already been handled", self.sent.await_args.args[1])
        self.db.expire_all()
        self.assertEqual(self.db.get(Customer, customer.id).wallet_balance, 1000 - 3 * PRICE)

    def test_cancel_charges_nothing_and_closes_the_photos(self):
        customer = self.make_customer(1000)
        gid = self.group(3)
        self.assertEqual(self.confirm(gid, bulk_orders.BULK_NO), [])
        self.db.expire_all()
        self.assertEqual(self.db.get(Customer, customer.id).wallet_balance, 1000)
        statuses = {r.status for r in self.db.query(WhatsAppIngestion).filter(WhatsAppIngestion.group_id == gid)}
        self.assertEqual(statuses, {"rejected"})

    def test_a_full_daily_limit_declines_before_charging(self):
        customer = self.make_customer(1000)
        gid = self.group(3)
        with patch.object(settings, "MAX_GENERATIONS_PER_DAY", 2):
            self.assertEqual(self.confirm(gid), [])
        self.db.expire_all()
        self.assertEqual(self.db.get(Customer, customer.id).wallet_balance, 1000)
        self.assertIn("nothing was charged", self.sent.await_args.args[1])

    def test_if_one_debit_fails_none_of_them_stand(self):
        customer = self.make_customer(1000)
        gid = self.group(3)
        real = meta_webhook.charge_customer_balance
        calls = {"n": 0}

        def flaky(*args, **kwargs):
            calls["n"] += 1
            if calls["n"] == 2:
                return False, 0
            return real(*args, **kwargs)

        with patch.object(meta_webhook, "charge_customer_balance", side_effect=flaky):
            self.assertEqual(self.confirm(gid), [])
        self.db.expire_all()
        self.assertEqual(self.db.get(Customer, customer.id).wallet_balance, 1000)
        self.assertEqual(self.db.query(WalletTransaction).filter(WalletTransaction.kind == KIND_DEBIT_ORDER).count(), 0)
        rows = self.db.query(WhatsAppIngestion).filter(WhatsAppIngestion.group_id == gid).all()
        self.assertEqual({r.status for r in rows}, {"awaiting_choice"})
        assert_ledger_matches_balances(self, self.db)

    def test_someone_elses_group_cannot_be_confirmed(self):
        self.make_customer(1000)
        gid = self.group(3)
        with patch.object(meta_webhook, "_same_sender", return_value=False):
            self.assertEqual(self.confirm(gid), [])


class RunnerTests(unittest.TestCase):
    def test_bulk_photos_run_a_few_at_a_time(self):
        running = peak = 0

        async def worker(_id):
            nonlocal running, peak
            running += 1
            peak = max(peak, running)
            await asyncio.sleep(0.01)
            running -= 1

        runs = [(worker, f"i{n}", None) for n in range(12)]
        with patch.object(settings, "BULK_CONCURRENCY", 3):
            asyncio.run(meta_webhook._run_bulk_jobs(runs))
        self.assertEqual(peak, 3)

    def test_one_failing_photo_does_not_stop_the_others(self):
        done = []

        async def worker(i):
            if i == "bad":
                raise RuntimeError("boom")
            done.append(i)

        asyncio.run(meta_webhook._run_bulk_jobs([(worker, "a", None), (worker, "bad", None), (worker, "b", None)]))
        self.assertEqual(sorted(done), ["a", "b"])


class IngestHoldTests(_Base):
    def _ingest(self, message_id):
        fake_upload = MagicMock()
        fake_upload.id = f"img-{message_id}"
        service = MagicMock()
        service.process_upload = AsyncMock(return_value=fake_upload)
        patches = [
            patch.object(meta_webhook, "get_media_url", new=AsyncMock(return_value="https://cdn/x.jpg")),
            patch.object(meta_webhook, "download_media", new=AsyncMock(return_value=(b"\xff\xd8\xff" + b"\x00" * 64, "image/jpeg"))),
            patch.object(meta_webhook, "validate_image", return_value=(True, None)),
            patch.object(meta_webhook, "check_image_quality", new=AsyncMock(return_value=MagicMock(approved=True))),
            patch.object(meta_webhook, "UploadService", return_value=service),
            patch.object(meta_webhook, "send_product_selection_buttons", new=self.choice_buttons),
            patch.object(bulk_orders, "schedule_prompt", new=self.schedule),
        ]
        for p in patches:
            p.start()
        try:
            event = {"sender": SENDER, "message_id": message_id, "media_id": "m1", "caption": "", "timestamp": "1"}
            return asyncio.run(meta_webhook._ingest_image_for_choice(self.db, event))
        finally:
            for p in reversed(patches):
                p.stop()

    def setUp(self):
        super().setUp()
        self.choice_buttons = AsyncMock(return_value=True)
        self.schedule = AsyncMock(return_value=True)
        self.make_customer(1000)

    def test_the_first_photo_gets_its_buttons_and_the_next_one_is_held_for_the_group_prompt(self):
        self._ingest("wamid.in.1")
        self.assertEqual(self.choice_buttons.await_count, 1)
        self.schedule.assert_not_awaited()
        self._ingest("wamid.in.2")
        self.assertEqual(self.choice_buttons.await_count, 1)               # no second per-photo message
        self.schedule.assert_awaited_once()

    def test_if_the_group_prompt_cannot_be_scheduled_the_photo_gets_its_normal_buttons(self):
        self.schedule.return_value = False
        self._ingest("wamid.in.1")
        self._ingest("wamid.in.2")
        self.assertEqual(self.choice_buttons.await_count, 2)

    def test_team_customers_keep_the_per_photo_flow(self):
        self.db.query(Customer).update({"tier": "ADMIN"})
        self.db.commit()
        self._ingest("wamid.in.1")
        self._ingest("wamid.in.2")
        self.assertEqual(self.choice_buttons.await_count, 2)
        self.schedule.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()


class RecoveryPatienceTests(_Base):
    def test_a_bulk_photo_waiting_its_turn_is_not_refunded_after_ten_minutes_but_after_thirty(self):
        from app.models.audit_log import AuditLog  # noqa: F401

        customer = self.make_customer(1000)
        single = self.make_ingestion(status="white_queued", message_id="wamid.rec.1")
        bulk = self.make_ingestion(status="white_queued", message_id="wamid.rec.2")
        bulk.group_id = "g-1"
        for row in (single, bulk):
            row.amount_charged = PRICE
            row.updated_at = datetime.now(timezone.utc) - timedelta(minutes=15)
        self.db.commit()
        with patch.object(mws, "send_whatsapp_text", new=AsyncMock(return_value=True)):
            self.assertEqual(asyncio.run(mws.recover_stuck_paid_orders(timedelta(minutes=10))), 1)
            self.db.expire_all()
            self.assertEqual((self.db.get(WhatsAppIngestion, single.id).status, self.db.get(WhatsAppIngestion, bulk.id).status),
                             ("failed", "white_queued"))
            bulk.updated_at = datetime.now(timezone.utc) - timedelta(minutes=35)
            self.db.commit()
            self.assertEqual(asyncio.run(mws.recover_stuck_paid_orders(timedelta(minutes=10))), 1)
        self.assertIsNotNone(customer)


class NeverStrandedTests(_Base):
    def setUp(self):
        super().setUp()
        self.single = AsyncMock(return_value=True)
        p = patch("app.services.meta_whatsapp_service.send_product_selection_buttons", new=self.single)
        p.start()
        self.addCleanup(p.stop)
        self.make_customer(1000)

    def test_a_held_photo_left_alone_gets_its_ordinary_buttons(self):
        # photo A was chosen by the customer meanwhile; photo B was held and is now the only one waiting
        a, b = self.photos(2)
        a.status = "choice_claimed"
        b.group_id = bulk_orders.HELD
        self.db.commit()
        self.assertFalse(asyncio.run(bulk_orders.send_group_prompt(SENDER)))
        self.assertEqual(self.single.await_count, 1)
        self.assertEqual(self.single.await_args.kwargs["ingestion_id"], b.id)
        self.db.expire_all()
        self.assertIsNone(self.db.get(WhatsAppIngestion, b.id).group_id)

    def test_a_failed_group_prompt_gives_every_photo_its_ordinary_buttons_again(self):
        rows = self.photos(3)
        for r in rows:
            r.group_id = bulk_orders.HELD
        self.db.commit()
        self.buttons.return_value = False
        self.assertFalse(asyncio.run(bulk_orders.send_group_prompt(SENDER)))
        self.assertEqual(self.single.await_count, 3)
        self.db.expire_all()
        self.assertEqual({r.group_id for r in self.db.query(WhatsAppIngestion)}, {None})

    def test_a_customer_still_sending_is_not_released(self):
        a, = self.photos(1, age_seconds=1)
        a.group_id = bulk_orders.HELD
        self.db.commit()
        asyncio.run(bulk_orders.send_group_prompt(SENDER))
        self.single.assert_not_awaited()
