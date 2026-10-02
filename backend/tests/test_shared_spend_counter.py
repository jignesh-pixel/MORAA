"""Phase 3 / COST-1: the daily ceiling is shared by every process (database), failed calls give their slot back,
pre-checks are counted, and a counter problem never blocks customers (in-process fallback)."""

import asyncio
import threading
import unittest
from unittest.mock import patch

from sqlalchemy.orm import sessionmaker

import app.models  # noqa: F401 -- register every model with Base.metadata
from app.ai import image_generation_manager as igm
from app.ai.image_generation_manager import ImageGenerationManager
from app.ai.providers.image_base import ImageGenerationResult
from app.config import settings
from app.database import Base
from app.models.generation_spend import GenerationSpend
from app.services import spend_counter
from tests.db_support import make_engine, postgres, requires_postgres

DAY = "2026-10-02"


class _CounterBase(unittest.TestCase):
    concurrent = True

    def setUp(self):
        self.engine = make_engine(concurrent=self.concurrent)
        Base.metadata.create_all(bind=self.engine)
        self.Session = sessionmaker(bind=self.engine)
        spend_counter.reset_for_tests()
        patcher = patch("app.database.SessionLocal", self.Session)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.addCleanup(self.engine.dispose)
        self._saved = (igm._spend_day, igm._spend_count)
        igm._spend_day, igm._spend_count = None, 0
        self.addCleanup(lambda: (setattr(igm, "_spend_day", self._saved[0]), setattr(igm, "_spend_count", self._saved[1])))
        for p in (patch.object(settings, "GENERATION_ENABLED", True), patch.object(settings, "MAX_GENERATIONS_PER_DAY", 10)):
            p.start()
            self.addCleanup(p.stop)

    def used(self, day=DAY):
        with self.Session() as db:
            row = db.get(GenerationSpend, day)
            return row.used if row else None


class CounterTests(_CounterBase):
    def test_reserve_up_to_the_cap_then_refuse_all_or_nothing(self):
        self.assertTrue(spend_counter.reserve(DAY, 6, 10))
        self.assertFalse(spend_counter.reserve(DAY, 6, 10))        # 12 > 10: nothing is taken
        self.assertEqual(self.used(), 6)
        self.assertTrue(spend_counter.reserve(DAY, 4, 10))
        self.assertEqual(self.used(), 10)
        self.assertFalse(spend_counter.reserve(DAY, 1, 10))

    def test_release_gives_slots_back_and_never_goes_below_zero(self):
        spend_counter.reserve(DAY, 5, 10)
        spend_counter.release(DAY, 2)
        self.assertEqual(self.used(), 3)
        spend_counter.release(DAY, 99)
        self.assertEqual(self.used(), 0)

    def test_a_new_day_starts_at_zero(self):
        spend_counter.reserve(DAY, 10, 10)
        self.assertTrue(spend_counter.reserve("2026-10-03", 1, 10))
        self.assertEqual(self.used("2026-10-03"), 1)

    def test_counting_without_a_ceiling_never_refuses(self):
        self.assertTrue(spend_counter.reserve(DAY, 50, None))
        self.assertEqual(spend_counter.used(DAY), 50)

    def test_missing_table_means_unknown_not_an_error(self):
        broken = make_engine()                                       # an engine with NO tables
        self.addCleanup(broken.dispose)
        with patch("app.database.SessionLocal", sessionmaker(bind=broken)):
            self.assertIsNone(spend_counter.reserve(DAY, 1, 10))
            self.assertIsNone(spend_counter.release(DAY, 1))
            self.assertIsNone(spend_counter.used(DAY))


@postgres
@requires_postgres
class CounterUnderRealLocksTests(_CounterBase):
    def test_100_concurrent_reservations_take_exactly_the_cap(self):
        results, errors = [], []
        barrier = threading.Barrier(100, timeout=120)

        def take():
            try:
                barrier.wait()
                results.append(spend_counter.reserve(DAY, 1, 40))
            except Exception as exc:
                errors.append(repr(exc))

        threads = [threading.Thread(target=take) for _ in range(100)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=180)
        self.assertEqual(errors, [])
        self.assertEqual(results.count(True), 40)
        self.assertEqual(results.count(False), 60)
        self.assertEqual(self.used(), 40)


class ManagerIntegrationTests(_CounterBase):
    def run_one(self, ok=True, spend_reserved=False):
        class Provider:
            is_available = True
            provider_name = "gemini"

            def supports_reference_image(self):
                return False

            async def generate_image(self, prompt, context=None, **kw):
                if ok:
                    return ImageGenerationResult(success=True, image_data=b"x" * 40, provider_name="gemini")
                return ImageGenerationResult(success=False, error="500 Internal Server Error", provider_name="gemini")

        manager = ImageGenerationManager()
        manager._get_provider_chain = lambda: ["gemini"]
        manager._get_provider = lambda name: Provider()
        return asyncio.run(manager.generate_image("p", {"request_id": "t"}, spend_reserved=spend_reserved))

    def test_a_successful_call_keeps_its_slot(self):
        self.assertTrue(self.run_one(ok=True).success)
        self.assertEqual(self.used(igm._today()), 1)

    def test_a_failed_call_gives_its_slot_back(self):
        self.assertFalse(self.run_one(ok=False).success)
        self.assertEqual(self.used(igm._today()), 0)

    def test_a_call_whose_slot_was_reserved_elsewhere_does_not_release_it(self):
        igm.reserve_generation_slots(6)                              # the pack reserved 6 up front
        self.assertFalse(self.run_one(ok=False, spend_reserved=True).success)
        self.assertEqual(self.used(igm._today()), 6)                 # released by the pack worker, not by the call

    def test_the_ceiling_is_shared_across_processes(self):
        """Another process's in-memory counter is empty, yet the shared count stops it at the cap."""
        for _ in range(10):
            self.assertIsNone(igm._spend_blocked())
        igm._spend_day, igm._spend_count = None, 0                   # "a second process" with a fresh memory counter
        self.assertIn("cap reached", igm._spend_blocked())

    def test_the_peek_does_not_consume(self):
        for _ in range(5):
            self.assertIsNone(igm.generation_capacity_blocked(10))
        self.assertEqual(self.used(igm._today()) or 0, 0)
        igm.reserve_generation_slots(10)
        self.assertIsNotNone(igm.generation_capacity_blocked(1))

    def test_prechecks_are_counted_but_never_blocked(self):
        for _ in range(15):                                          # more than the cap of 10
            igm.record_external_spend(1)
        self.assertEqual(self.used(igm._today()), 15)

    def test_release_helper_hands_slots_back(self):
        igm.reserve_generation_slots(6)
        igm.release_generation_slots(4)
        self.assertEqual(self.used(igm._today()), 2)

    def test_a_release_after_midnight_credits_the_day_that_was_charged(self):
        spend_counter.reserve("2026-10-02", 6, 10)
        spend_counter.reserve("2026-10-03", 2, 10)
        igm.release_generation_slots(4, "2026-10-02")
        self.assertEqual((self.used("2026-10-02"), self.used("2026-10-03")), (2, 2))

    def test_a_cancelled_call_gives_its_slot_back(self):
        class Hangs:
            is_available = True
            provider_name = "gemini"

            def supports_reference_image(self):
                return False

            async def generate_image(self, *a, **k):
                await asyncio.sleep(3600)

        async def run():
            manager = ImageGenerationManager()
            manager._get_provider_chain = lambda: ["gemini"]
            manager._get_provider = lambda name: Hangs()
            task = asyncio.ensure_future(manager.generate_image("p", {"request_id": "t"}))
            await asyncio.sleep(0.2)
            self.assertEqual(self.used(igm._today()), 1)             # the slot was taken
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task

        asyncio.run(run())
        self.assertEqual(self.used(igm._today()), 0)                 # and returned: nothing was produced

    def test_without_the_table_the_in_process_counter_still_enforces_the_cap(self):
        broken = make_engine()
        self.addCleanup(broken.dispose)
        with patch("app.database.SessionLocal", sessionmaker(bind=broken)):
            for _ in range(10):
                self.assertIsNone(igm._spend_blocked())
            self.assertIn("cap reached", igm._spend_blocked())
            igm.release_generation_slots(3)
            self.assertIsNone(igm._spend_blocked())


if __name__ == "__main__":
    unittest.main()
