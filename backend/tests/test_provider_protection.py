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
