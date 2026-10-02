"""EXT-6: circuit breaker and token bucket around image providers."""

import asyncio
import unittest
from unittest.mock import AsyncMock, patch

from app.ai import image_generation_manager as igm
from app.ai.provider_protection import CircuitBreaker, TokenBucket
from app.ai.providers.image_base import ImageGenerationResult
from app.config import settings


class BreakerTests(unittest.TestCase):
    def setUp(self):
        self.cb = CircuitBreaker()
        p1 = patch.object(settings, "CIRCUIT_BREAKER_FAILURES", 3)
        p2 = patch.object(settings, "CIRCUIT_BREAKER_COOLDOWN_SECONDS", 60.0)
        p1.start(), p2.start()
        self.addCleanup(p1.stop)
        self.addCleanup(p2.stop)

    def test_opens_after_n_failures_in_a_row(self):
        for _ in range(2):
            self.cb.record_outage_failure("g", now=0)
        self.assertTrue(self.cb.allow("g", now=1))
        self.cb.record_outage_failure("g", now=1)
        self.assertFalse(self.cb.allow("g", now=2))

    def test_a_success_resets_the_count(self):
        self.cb.record_outage_failure("g", now=0)
        self.cb.record_outage_failure("g", now=0)
        self.cb.record_success("g")
        self.cb.record_outage_failure("g", now=0)
        self.assertTrue(self.cb.allow("g", now=1))

    def test_one_probe_after_cooldown_then_close_on_success(self):
        for _ in range(3):
            self.cb.record_outage_failure("g", now=0)
        self.assertFalse(self.cb.allow("g", now=30))
        self.assertTrue(self.cb.allow("g", now=61))          # the probe
        self.assertFalse(self.cb.allow("g", now=62))         # only one at a time
        self.cb.record_success("g")
        self.assertTrue(self.cb.allow("g", now=63))

    def test_a_failed_probe_restarts_the_cooldown(self):
        for _ in range(3):
            self.cb.record_outage_failure("g", now=0)
        self.assertTrue(self.cb.allow("g", now=61))
        self.cb.record_outage_failure("g", now=61)
        self.assertFalse(self.cb.allow("g", now=100))
        self.assertTrue(self.cb.allow("g", now=122))

    def test_zero_turns_it_off(self):
        with patch.object(settings, "CIRCUIT_BREAKER_FAILURES", 0):
            for _ in range(20):
                self.cb.record_outage_failure("g", now=0)
            self.assertTrue(self.cb.allow("g", now=1))

    def test_providers_are_independent(self):
        for _ in range(3):
            self.cb.record_outage_failure("gemini", now=0)
        self.assertTrue(self.cb.allow("openai", now=1))


class BucketTests(unittest.TestCase):
    def test_off_by_default_never_waits(self):
        self.assertTrue(asyncio.run(TokenBucket().acquire("g")))

    def test_a_burst_beyond_capacity_fails_fast_when_the_wait_is_too_long(self):
        with patch.object(settings, "IMAGE_PROVIDER_RPM", 6), patch.object(settings, "IMAGE_RATE_WAIT_MAX_SECONDS", 0.0):
            b = TokenBucket()
            results = [asyncio.run(b.acquire("g")) for _ in range(8)]
        self.assertEqual(results[:6], [True] * 6)            # capacity = rpm (max 10)
        self.assertFalse(all(results))


class ManagerIntegrationTests(unittest.TestCase):
    def _manager(self, provider):
        m = igm.ImageGenerationManager()
        m._providers = {"gemini": provider}
        m._initialised = True
        return m

    def test_five_timeouts_open_the_circuit_and_the_sixth_call_never_reaches_the_provider(self):
        provider = AsyncMock()
        provider.provider_name = "gemini"
        provider.generate_image = AsyncMock(return_value=ImageGenerationResult(
            success=False, error="Image provider timeout: deadline exceeded", provider_name="gemini"))
        m = self._manager(provider)
        with patch.object(settings, "IMAGE_RATE_LIMIT_RETRIES", 0):
            for _ in range(5):
                asyncio.run(m._attempt_with_retries(provider, "gemini", "p", {}, None, "image/jpeg", "r"))
            calls = provider.generate_image.await_count
            result = asyncio.run(m._attempt_with_retries(provider, "gemini", "p", {}, None, "image/jpeg", "r"))
        self.assertEqual(calls, 5)
        self.assertEqual(provider.generate_image.await_count, 5)
        self.assertFalse(result.success)
        self.assertIn("circuit open", result.error)

    def test_a_blocked_prompt_never_counts_against_the_provider(self):
        provider = AsyncMock()
        provider.generate_image = AsyncMock(return_value=ImageGenerationResult(
            success=False, error="prompt blocked by safety filter", provider_name="gemini"))
        m = self._manager(provider)
        with patch.object(settings, "IMAGE_RATE_LIMIT_RETRIES", 0):
            for _ in range(10):
                asyncio.run(m._attempt_with_retries(provider, "gemini", "p", {}, None, "image/jpeg", "r"))
        self.assertEqual(provider.generate_image.await_count, 10)


if __name__ == "__main__":
    unittest.main()


class BreakerRobustnessTests(unittest.TestCase):
    def _manager(self, provider):
        m = igm.ImageGenerationManager()
        m._providers = {"gemini": provider}
        m._initialised = True
        return m

    def _open_the_circuit(self, m, provider):
        provider.generate_image = AsyncMock(return_value=ImageGenerationResult(
            success=False, error="Image provider timeout: deadline exceeded", provider_name="gemini"))
        with patch.object(settings, "IMAGE_RATE_LIMIT_RETRIES", 0):
            for _ in range(5):
                asyncio.run(m._attempt_with_retries(provider, "gemini", "p", {}, None, "image/jpeg", "r"))

    def test_a_cancelled_probe_never_wedges_the_circuit(self):
        provider = AsyncMock()
        m = self._manager(provider)
        self._open_the_circuit(m, provider)
        igm.breaker._opened_at["gemini"] -= 1000            # cool-down over: the next call is the probe

        async def hang(*a, **k):
            await asyncio.sleep(30)

        provider.generate_image = hang

        async def probe_then_cancel():
            task = asyncio.create_task(m._attempt_with_retries(provider, "gemini", "p", {}, None, "image/jpeg", "r"))
            await asyncio.sleep(0.05)
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

        asyncio.run(probe_then_cancel())
        self.assertTrue(igm.breaker.allow("gemini"))          # the probe slot was given back

    def test_a_probe_that_never_reports_expires(self):
        cb = CircuitBreaker()
        with patch.object(settings, "CIRCUIT_BREAKER_FAILURES", 1), patch.object(settings, "CIRCUIT_BREAKER_COOLDOWN_SECONDS", 60.0):
            cb.record_outage_failure("g", now=0)
            self.assertTrue(cb.allow("g", now=61))             # probe taken, never reports
            self.assertFalse(cb.allow("g", now=100))
            self.assertTrue(cb.allow("g", now=125))            # a lost probe does not block forever

    def test_a_provider_that_raises_counts_as_an_outage(self):
        provider = AsyncMock()
        provider.generate_image = AsyncMock(side_effect=RuntimeError("socket closed"))
        m = self._manager(provider)
        with patch.object(settings, "IMAGE_RATE_LIMIT_RETRIES", 0), patch.object(settings, "CIRCUIT_BREAKER_FAILURES", 2):
            for _ in range(2):
                with self.assertRaises(RuntimeError):
                    asyncio.run(m._attempt_with_retries(provider, "gemini", "p", {}, None, "image/jpeg", "r"))
            self.assertTrue(igm.breaker.is_open("gemini"))

    def test_retries_inside_one_order_count_as_one_failure(self):
        provider = AsyncMock()
        provider.generate_image = AsyncMock(return_value=ImageGenerationResult(
            success=False, error="429 Too Many Requests per minute", provider_name="gemini"))
        m = self._manager(provider)
        with patch.object(settings, "IMAGE_RATE_LIMIT_RETRIES", 2), patch.object(settings, "IMAGE_RETRY_BACKOFF_BASE_SECONDS", 0.0), \
             patch.object(settings, "IMAGE_RETRY_BACKOFF_CAP_SECONDS", 0.0), patch.object(settings, "CIRCUIT_BREAKER_FAILURES", 3):
            for _ in range(2):
                asyncio.run(m._attempt_with_retries(provider, "gemini", "p", {}, None, "image/jpeg", "r"))
            self.assertEqual(provider.generate_image.await_count, 6)       # 2 orders x 3 attempts
            self.assertFalse(igm.breaker.is_open("gemini"))               # but only 2 failures were counted

    def test_our_own_rate_limit_is_never_the_providers_fault(self):
        provider = AsyncMock()
        m = self._manager(provider)
        with patch.object(settings, "IMAGE_PROVIDER_RPM", 6), patch.object(settings, "IMAGE_RATE_WAIT_MAX_SECONDS", 0.0), \
             patch.object(settings, "IMAGE_RATE_LIMIT_RETRIES", 0), patch.object(settings, "CIRCUIT_BREAKER_FAILURES", 2):
            provider.generate_image = AsyncMock(return_value=ImageGenerationResult(success=True, provider_name="gemini"))
            for _ in range(20):
                asyncio.run(m._attempt_with_retries(provider, "gemini", "p", {}, None, "image/jpeg", "r"))
        self.assertFalse(igm.breaker.is_open("gemini"))

    def test_a_non_recoverable_error_that_mentions_a_connection_is_not_an_outage(self):
        self.assertFalse(igm._is_provider_outage("prompt blocked by safety filter after connection reset"))
        self.assertTrue(igm._is_provider_outage("connection reset by peer"))
