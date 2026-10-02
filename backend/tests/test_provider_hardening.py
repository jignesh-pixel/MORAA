"""Phase 3 / EXT-1, EXT-2, EXT-3, PERF-3, PERF-4: provider timeouts, rate-limit retries, shared clients.

Image prompts and request contents are untouched (locked by the prompt snapshot); only the transport changed.
"""

import asyncio
import time
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from app.ai import image_generation_manager as igm
from app.ai.image_generation_manager import (
    ImageGenerationManager,
    _is_quota_exhaustion,
    _is_retryable_rate_limit,
    _retry_delay_seconds,
)
from app.ai.providers import gemini_image_provider as gem
from app.ai.providers import openai_image_provider as oai
from app.ai.providers.image_base import ImageGenerationResult
from app.config import settings

GEMINI_PER_MINUTE_429 = (
    "429 RESOURCE_EXHAUSTED. {'error': {'code': 429, 'message': 'You exceeded your current quota, please check your "
    "plan and billing details. * Quota exceeded for metric: generate_content_free_tier_requests, limit: 15, "
    "GenerateRequestsPerMinutePerProjectPerModel-FreeTier. Please retry in 34.3s.', 'status': 'RESOURCE_EXHAUSTED'}}"
)
GEMINI_PER_DAY_429 = (
    "429 RESOURCE_EXHAUSTED. You exceeded your current quota. GenerateRequestsPerDayPerProjectPerModel-FreeTier, limit: 0"
)


class ClassificationTests(unittest.TestCase):
    def test_real_billing_and_daily_exhaustion_halt(self):
        for text in ("429 insufficient_quota: check your plan and billing details",
                     "credit_balance_exhausted", GEMINI_PER_DAY_429, "Billing hard limit has been reached",
                     "billing_not_active"):
            self.assertTrue(_is_quota_exhaustion(text), text)
            self.assertFalse(_is_retryable_rate_limit(text), text)

    def test_per_minute_rate_limits_are_retried_not_halted(self):
        for text in ("429 Too Many Requests: rate limit reached, retry later",
                     "429 RESOURCE_EXHAUSTED. Quota exceeded for metric generate_content requests per minute",
                     "503 Service Unavailable: the model is overloaded", "429 RESOURCE_EXHAUSTED",
                     "429 You exceeded your current quota"):
            self.assertFalse(_is_quota_exhaustion(text), text)
            self.assertTrue(_is_retryable_rate_limit(text), text)

    def test_other_errors_are_not_retried(self):
        for text in ("Image provider timeout: deadline exceeded after 130s", "500 Internal Server Error",
                     "400 Bad Request: invalid prompt", "Gemini returned a response but no image data was found."):
            self.assertFalse(_is_retryable_rate_limit(text), text)

    def test_backoff_grows_is_jittered_and_capped(self):
        with patch.object(settings, "IMAGE_RETRY_BACKOFF_BASE_SECONDS", 2.0), \
             patch.object(settings, "IMAGE_RETRY_BACKOFF_CAP_SECONDS", 15.0):
            first = [_retry_delay_seconds("429", 0) for _ in range(200)]
            later = [_retry_delay_seconds("429", 6) for _ in range(200)]
        self.assertTrue(all(1.0 <= d <= 3.0 for d in first))          # 2 s +- 50 %
        self.assertTrue(all(7.5 <= d <= 22.5 for d in later))          # capped at 15 s +- 50 %
        self.assertGreater(len({round(d, 3) for d in first}), 50)      # jitter: not in lockstep

    def test_billing_hard_limit_code_halts(self):
        self.assertTrue(_is_quota_exhaustion("Error code: 429 - {'error': {'code': 'billing_hard_limit_reached'}}"))

    def test_never_retries_earlier_than_the_provider_asked(self):
        with patch.object(settings, "IMAGE_RETRY_BACKOFF_BASE_SECONDS", 2.0), \
             patch.object(settings, "IMAGE_RETRY_BACKOFF_CAP_SECONDS", 15.0):
            delays = [_retry_delay_seconds("429 retry in 10s", 0) for _ in range(300)]
        self.assertGreaterEqual(min(delays), 10.0)

    def test_a_long_provider_hint_means_move_on_instead_of_waiting(self):
        with patch.object(settings, "IMAGE_RETRY_BACKOFF_CAP_SECONDS", 15.0):
            self.assertIsNone(_retry_delay_seconds(GEMINI_PER_MINUTE_429, 0))        # "retry in 34.3s"
            self.assertIsNotNone(_retry_delay_seconds("429 retry in 3s", 0))


class _Scripted:
    """Provider that answers from a script of results (or sleeps forever for 'hang')."""

    is_available = True

    def __init__(self, name, script):
        self.provider_name = name
        self.script = list(script)
        self.calls = 0

    def supports_reference_image(self):
        return True

    async def generate_image(self, prompt, context=None, reference_image=None, reference_mime_type="image/jpeg"):
        self.calls += 1
        step = self.script.pop(0) if self.script else self.script_default
        if step == "hang":
            await asyncio.sleep(3600)
        if step == "ok":
            return ImageGenerationResult(success=True, image_url="data:image/png;base64,QUJD" * 4, provider_name=self.provider_name)
        return ImageGenerationResult(success=False, error=step, provider_name=self.provider_name)

    script_default = "500 Internal Server Error"


class ManagerRetryTests(unittest.TestCase):
    def setUp(self):
        for p in (patch.object(settings, "IMAGE_RETRY_BACKOFF_BASE_SECONDS", 0.001),
                  patch.object(settings, "IMAGE_RETRY_BACKOFF_CAP_SECONDS", 0.01),
                  patch.object(settings, "IMAGE_RATE_LIMIT_RETRIES", 2),
                  patch.object(settings, "MAX_GENERATIONS_PER_DAY", 100000),
                  patch.object(settings, "GENERATION_ENABLED", True)):
            p.start()
            self.addCleanup(p.stop)
        self._saved = (igm._spend_day, igm._spend_count)
        igm._spend_day, igm._spend_count = None, 0
        self.addCleanup(lambda: (setattr(igm, "_spend_day", self._saved[0]), setattr(igm, "_spend_count", self._saved[1])))

    def run_manager(self, primary, fallback=None):
        manager = ImageGenerationManager()
        providers = {"gemini": primary, "openai": fallback or _Scripted("openai", ["ok"])}
        manager._get_provider_chain = lambda: ["gemini", "openai"]
        manager._get_provider = lambda name: providers[name]
        return asyncio.run(manager.generate_image("p", {"request_id": "t"}, reference_image=b"x"))

    def test_a_rate_limit_is_retried_on_the_same_provider_then_succeeds(self):
        primary = _Scripted("gemini", ["429 Too Many Requests", "429 Too Many Requests", "ok"])
        fallback = _Scripted("openai", ["ok"])
        result = self.run_manager(primary, fallback)
        self.assertTrue(result.success)
        self.assertEqual((primary.calls, fallback.calls), (3, 0))
        self.assertFalse(result.fallback_used)

    def test_retries_are_bounded_then_the_fallback_provider_is_used(self):
        primary = _Scripted("gemini", ["429 Too Many Requests"] * 10)
        fallback = _Scripted("openai", ["ok"])
        result = self.run_manager(primary, fallback)
        self.assertTrue(result.success)
        self.assertEqual((primary.calls, fallback.calls), (3, 1))     # 1 call + 2 retries, then fallback
        self.assertTrue(result.fallback_used)

    def test_billing_exhaustion_is_never_retried_and_never_falls_back(self):
        primary = _Scripted("gemini", ["429 insufficient_quota: billing"] * 5)
        fallback = _Scripted("openai", ["ok"])
        result = self.run_manager(primary, fallback)
        self.assertFalse(result.success)
        self.assertEqual((primary.calls, fallback.calls), (1, 0))

    def test_a_server_error_falls_back_without_retrying(self):
        primary = _Scripted("gemini", ["500 Internal Server Error"] * 5)
        fallback = _Scripted("openai", ["ok"])
        self.run_manager(primary, fallback)
        self.assertEqual((primary.calls, fallback.calls), (1, 1))

    def test_a_hung_provider_is_cut_off_and_the_fallback_serves_the_order(self):
        primary = _Scripted("gemini", ["hang"])
        fallback = _Scripted("openai", ["ok"])
        with patch.object(settings, "IMAGE_PROVIDER_TIMEOUT_SECONDS", 0.2):
            started = time.monotonic()
            result = self.run_manager(primary, fallback)
        self.assertLess(time.monotonic() - started, 5)
        self.assertTrue(result.success)
        self.assertEqual(fallback.calls, 1)

    def test_retries_do_not_use_extra_spend_slots(self):
        primary = _Scripted("gemini", ["429 Too Many Requests", "429 Too Many Requests", "ok"])
        with patch.object(settings, "MAX_GENERATIONS_PER_DAY", 5):
            manager = ImageGenerationManager()
            manager._get_provider_chain = lambda: ["gemini"]
            manager._get_provider = lambda name: primary
            asyncio.run(manager.generate_image("p", {"request_id": "t"}))
        self.assertEqual(igm._spend_count, 1)                           # one order = one slot, whatever the retries


class PackDeadlineTests(unittest.TestCase):
    def run_pack(self, coros, deadline):
        from app.services.meta_whatsapp_service import _gather_styles_with_deadline

        return asyncio.run(_gather_styles_with_deadline(coros, deadline, "ing-1"))

    @staticmethod
    async def style(value, delay):
        await asyncio.sleep(delay)
        return value

    @staticmethod
    async def boom():
        raise RuntimeError("provider exploded")

    def test_finished_styles_ship_and_a_stuck_style_is_dropped_at_the_deadline(self):
        started = time.monotonic()
        results = self.run_pack(
            [self.style("a", 0.01), self.style("stuck", 3600), self.style("c", 0.02)], deadline=0.5)
        self.assertLess(time.monotonic() - started, 5)
        self.assertEqual(results, ["a", None, "c"])                    # same order as the styles were asked for

    def test_cancelling_the_worker_cancels_the_styles_too(self):
        from app.services.meta_whatsapp_service import _gather_styles_with_deadline

        state = {"cancelled": 0}

        async def slow():
            try:
                await asyncio.sleep(3600)
            except asyncio.CancelledError:
                state["cancelled"] += 1
                raise

        async def main():
            worker = asyncio.ensure_future(_gather_styles_with_deadline([slow(), slow(), slow()], 0, "ing-1"))
            await asyncio.sleep(0.05)
            worker.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await worker

        asyncio.run(main())
        self.assertEqual(state["cancelled"], 3)                         # no paid call is left running unattended

    def test_a_failing_style_is_none_not_an_exception(self):
        self.assertEqual(self.run_pack([self.style("a", 0), self.boom()], deadline=5), ["a", None])

    def test_no_deadline_means_wait_for_everyone(self):
        self.assertEqual(self.run_pack([self.style("a", 0.05), self.style("b", 0.1)], deadline=0), ["a", "b"])

    def test_empty_pack(self):
        self.assertEqual(self.run_pack([], deadline=1), [])


class _FakeGenaiTypes:
    class HttpOptions:
        def __init__(self, timeout=None):
            self.timeout = timeout


class _FakeGenai:
    created = []

    class Client:
        def __init__(self, api_key=None, http_options=None):
            _FakeGenai.created.append((api_key, http_options))


class SharedClientTests(unittest.TestCase):
    def test_gemini_client_is_created_once_per_loop_with_the_timeout(self):
        _FakeGenai.created.clear()

        async def two_calls():
            a = gem._get_client(_FakeGenai, _FakeGenaiTypes)
            b = gem._get_client(_FakeGenai, _FakeGenaiTypes)
            return a is b

        with patch.object(settings, "GEMINI_API_KEY", "k1"), patch.object(settings, "GEMINI_IMAGE_TIMEOUT_SECONDS", 90.0):
            self.assertTrue(asyncio.run(two_calls()))
        self.assertEqual(len(_FakeGenai.created), 1)
        self.assertEqual(_FakeGenai.created[0][1].timeout, 90000)

    def test_openai_client_is_shared_and_has_a_short_connect_timeout(self):
        made = []

        class FakeAsyncOpenAI:
            def __init__(self, **kw):
                made.append(kw)

        async def two():
            return oai._get_client(FakeAsyncOpenAI) is oai._get_client(FakeAsyncOpenAI)

        with patch.object(settings, "OPENAI_API_KEY", "k2"), \
             patch.object(settings, "OPENAI_IMAGE_TIMEOUT_SECONDS", 120.0), \
             patch.object(settings, "OPENAI_IMAGE_CONNECT_TIMEOUT_SECONDS", 10.0):
            self.assertTrue(asyncio.run(two()))
        self.assertEqual(len(made), 1)
        timeout = made[0]["timeout"]
        self.assertEqual((timeout.read, timeout.connect, made[0]["max_retries"]), (120.0, 10.0, 0))


class GeminiNativeAsyncTests(unittest.TestCase):
    """The real GeminiImageProvider path with a stubbed SDK: native async, no worker threads."""

    def test_100_concurrent_calls_finish_together_and_use_no_threads(self):
        import threading

        class Models:
            async def generate_content(self, model, contents, config):
                await asyncio.sleep(0.05)
                part = SimpleNamespace(inline_data=SimpleNamespace(data=b"img", mime_type="image/png"))
                return SimpleNamespace(candidates=[SimpleNamespace(content=SimpleNamespace(parts=[part]))])

        class Aio:
            models = Models()

        client = SimpleNamespace(aio=Aio())
        from google import genai  # noqa: F401 -- the SDK is imported lazily on first use; keep that out of the timing
        from google.genai import types  # noqa: F401
        before = threading.active_count()

        async def run():
            provider = gem.GeminiImageProvider()
            quiet = SimpleNamespace(info=lambda *a, **k: None, warning=lambda *a, **k: None, error=lambda *a, **k: None)
            with patch.object(gem, "_get_client", return_value=client), patch.object(gem, "logger", quiet), \
                 patch.object(settings, "GEMINI_API_KEY", "k"):
                started = time.monotonic()
                results = await asyncio.gather(*[provider.generate_image("p", {"request_id": str(i)}) for i in range(100)])
                return time.monotonic() - started, results, threading.active_count()

        elapsed, results, threads_during = asyncio.run(run())
        self.assertTrue(all(r.success for r in results))
        self.assertLess(elapsed, 1.0)                                  # 100 calls overlap; they do not queue on a pool
        self.assertLessEqual(threads_during, before + 2)


if __name__ == "__main__":
    unittest.main()
