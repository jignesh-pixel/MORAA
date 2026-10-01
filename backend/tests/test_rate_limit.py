"""Phase 0 / U4: the rate limiter never throttles signed webhooks, health or static images."""

import unittest
from unittest.mock import patch

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.config import settings
from app.middleware.rate_limit import RateLimitMiddleware, is_rate_limit_exempt, rate_limit_bucket


def _app(limit: int = 5, window: int = 60, webhook_limit: int = 3000) -> FastAPI:
    app = FastAPI()

    @app.post("/api/meta/webhook")
    async def meta():
        return {"ok": True}

    @app.post("/api/payments/razorpay/webhook")
    async def razorpay():
        return {"ok": True}

    @app.get("/health")
    async def health():
        return {"ok": True}

    @app.get("/uploads/{rid}/{name}")
    async def upload(rid: str, name: str):
        return {"ok": True}

    @app.get("/api/history")
    async def history():
        return {"ok": True}

    with patch.object(settings, "RATE_LIMIT_REQUESTS", limit), \
         patch.object(settings, "WEBHOOK_RATE_LIMIT_REQUESTS", webhook_limit), \
         patch.object(settings, "RATE_LIMIT_WINDOW_SECONDS", window):
        app.add_middleware(RateLimitMiddleware)
        # Build the middleware stack while the patched limits are active.
        app.middleware_stack = app.build_middleware_stack()
    return app


class RateLimitExemptionTests(unittest.TestCase):
    def setUp(self):
        self._enabled = patch.object(settings, "RATE_LIMIT_ENABLED", True)
        self._enabled.start()
        self.client = TestClient(_app(limit=5))

    def tearDown(self):
        self._enabled.stop()

    def test_150_meta_webhooks_from_one_ip_never_429(self):
        codes = {self.client.post("/api/meta/webhook", json={}).status_code for _ in range(150)}
        self.assertEqual(codes, {200})

    def test_razorpay_webhook_never_429(self):
        codes = {self.client.post("/api/payments/razorpay/webhook/", json={}).status_code for _ in range(20)}
        self.assertNotIn(429, codes)

    def test_health_and_uploads_never_429(self):
        for _ in range(20):
            self.assertEqual(self.client.get("/health").status_code, 200)
            self.assertEqual(self.client.get("/uploads/abc/x.png").status_code, 200)

    def test_other_routes_still_limited(self):
        codes = [self.client.get("/api/history").status_code for _ in range(7)]
        self.assertEqual(codes[:5], [200] * 5)
        self.assertEqual(codes[5:], [429, 429])

    def test_webhook_traffic_does_not_consume_the_dashboard_budget(self):
        for _ in range(50):
            self.client.post("/api/meta/webhook", json={})
        self.assertEqual(self.client.get("/api/history").status_code, 200)

    def test_webhooks_have_their_own_finite_budget(self):
        client = TestClient(_app(limit=5, webhook_limit=10))
        codes = [client.post("/api/meta/webhook", json={}).status_code for _ in range(12)]
        self.assertEqual(codes[:10], [200] * 10)
        self.assertEqual(codes[10:], [429, 429])
        # Razorpay shares the webhook bucket for that IP; the dashboard does not.
        self.assertEqual(client.post("/api/payments/razorpay/webhook", json={}).status_code, 429)
        self.assertEqual(client.get("/api/history").status_code, 200)


class BucketTests(unittest.TestCase):
    def test_paths(self):
        for path in ("/health", "/uploads/a/b.jpg"):
            self.assertTrue(is_rate_limit_exempt(path), path)
            self.assertIsNone(rate_limit_bucket(path), path)
        for path in ("/api/meta/webhook", "/api/meta/webhook/", "/api/payments/razorpay/webhook"):
            self.assertEqual(rate_limit_bucket(path), "webhook", path)
        for path in ("/api/meta/webhook/retry/x", "/api/history", "/uploads", "/api/generate-image",
                     "/healthz", "/api/meta/webhook/status/x"):
            self.assertEqual(rate_limit_bucket(path), "api", path)


class PruneTests(unittest.TestCase):
    def test_idle_ips_are_forgotten(self):
        with patch.object(settings, "RATE_LIMIT_WINDOW_SECONDS", 60):
            mw = RateLimitMiddleware(FastAPI())
        mw._request_counts[("api", "1.1.1.1")] = [100.0]
        mw._request_counts[("webhook", "2.2.2.2")] = [150.0]
        mw._request_counts[("api", "3.3.3.3")] = []
        mw._prune(now=170.0)
        self.assertEqual(set(mw._request_counts), {("webhook", "2.2.2.2")})

    def test_prune_runs_at_most_once_per_window(self):
        with patch.object(settings, "RATE_LIMIT_WINDOW_SECONDS", 60):
            mw = RateLimitMiddleware(FastAPI())
        mw._prune(now=1000.0)
        mw._request_counts[("api", "1.1.1.1")] = [0.0]
        mw._prune(now=1010.0)
        self.assertIn(("api", "1.1.1.1"), mw._request_counts)
        mw._prune(now=1061.0)
        self.assertNotIn(("api", "1.1.1.1"), mw._request_counts)


if __name__ == "__main__":
    unittest.main()
