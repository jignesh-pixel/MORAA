"""Production safety fixes (28 Sep 2026 audit). All network calls mocked."""

import asyncio
import hashlib
import hmac
import json
import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool


from tests.db_support import make_engine
from app.config import settings
from app.database import Base
from app.main import app
from app.models.audit_log import AuditLog
from app.models.customer import Customer
from app.models.image import Image
from app.models.whatsapp_ingestion import PRODUCT_PACK_1, PRODUCT_WHITE_BG, WhatsAppIngestion
from app.services import meta_whatsapp_service as mws

NGROK = "drainpipe-unsoiled-native.ngrok-free.dev"
SENDER = "919000000001"


# ── 1/10. Meta webhook signature ───────────────────────────────────────────
class MetaSignatureTests(unittest.TestCase):
    def setUp(self):
        self.client = TestClient(app)
        # Statuses-only payload: exercises signature + parsing, no side effects.
        self.body = json.dumps({"object": "whatsapp_business_account", "entry": [
            {"changes": [{"field": "messages", "value": {"statuses": [{"id": "x", "status": "read"}]}}]}]}).encode()

    def _post(self, signature=None, host=NGROK):
        headers = {"host": host, "content-type": "application/json"}
        if signature is not None:
            headers["X-Hub-Signature-256"] = signature
        return self.client.post("/api/meta/webhook", content=self.body, headers=headers).json()

    def test_get_verification_through_public_host(self):
        with patch.object(settings, "META_VERIFY_TOKEN", "tok"):
            r = self.client.get("/api/meta/webhook", headers={"host": NGROK},
                                params={"hub.mode": "subscribe", "hub.verify_token": "tok", "hub.challenge": "77"})
        self.assertEqual((r.status_code, r.text), (200, "77"))

    def test_valid_signature_processed(self):
        sig = "sha256=" + hmac.new(b"app-secret", self.body, hashlib.sha256).hexdigest()
        with patch.object(settings, "META_APP_SECRET", "app-secret"):
            self.assertEqual(self._post(sig).get("status"), "ok")

    def test_invalid_and_missing_signature_rejected(self):
        with patch.object(settings, "META_APP_SECRET", "app-secret"):
            self.assertEqual(self._post("sha256=deadbeef")["message"], "Invalid signature")
            self.assertEqual(self._post(None)["message"], "Invalid signature")

    def test_missing_secret_rejected_in_production(self):
        with patch.object(settings, "META_APP_SECRET", ""), patch.object(settings, "ALLOW_UNSIGNED_WEBHOOKS", False):
            self.assertEqual(self._post(None)["message"], "Webhook not configured")

    def test_missing_secret_accepted_only_with_dev_flag(self):
        with patch.object(settings, "META_APP_SECRET", ""), patch.object(settings, "ALLOW_UNSIGNED_WEBHOOKS", True):
            self.assertEqual(self._post(None).get("status"), "ok")

    def test_debug_alone_never_accepts_unsigned(self):
        with patch.object(settings, "META_APP_SECRET", ""), \
             patch.object(settings, "ALLOW_UNSIGNED_WEBHOOKS", False), \
             patch.object(settings, "DEBUG", True):
            self.assertEqual(self._post(None)["message"], "Webhook not configured")

    def test_verify_function_fails_closed_without_secret(self):
        with patch.object(settings, "META_APP_SECRET", ""):
            self.assertFalse(mws.verify_webhook_signature(b"body", "sha256=abc"))


# ── 2. Public host guard ───────────────────────────────────────────────────
class PublicHostGuardTests(unittest.TestCase):
    def setUp(self):
        self.client = TestClient(app)

    def test_public_paid_routes_blocked(self):
        for path in ("/api/generate-image", "/api/analyze/sync", "/api/upload", "/api/auth/signup",
                     "/api/meta/webhook/retry/x"):
            r = self.client.post(path, json={}, headers={"host": NGROK})
            self.assertEqual(r.status_code, 404, path)
        for path in ("/api/history", "/docs", "/uploads/a/b.jpg", "/api/meta/webhook/status/x", "/"):
            self.assertEqual(self.client.get(path, headers={"host": NGROK}).status_code, 404, path)

    def test_tunnel_with_local_host_header_still_blocked(self):
        r = self.client.post("/api/generate-image", json={},
                             headers={"host": "localhost:8000", "x-forwarded-for": "1.2.3.4"})
        self.assertEqual(r.status_code, 404)

    def test_localhost_unaffected(self):
        for host in ("localhost:8000", "127.0.0.1:8000", "[::1]:8000"):
            r = self.client.get("/api/generate-image/health", headers={"host": host})
            self.assertEqual(r.status_code, 200, host)
        # Validation error (422), not the guard's 404: route reached.
        r = self.client.post("/api/generate-image", json={}, headers={"host": "localhost:8000"})
        self.assertEqual(r.status_code, 422)

    def test_razorpay_unsigned_rejected_without_dev_flag_even_in_debug(self):
        from app.api.routes.payment_routes import verify_razorpay_signature

        with patch.object(settings, "RAZORPAY_WEBHOOK_SECRET", ""), \
             patch.object(settings, "ALLOW_UNSIGNED_WEBHOOKS", False), \
             patch.object(settings, "DEBUG", True):
            r = self.client.post("/api/payments/razorpay/webhook", json={"event": "payment.captured"},
                                 headers={"host": NGROK})
            self.assertEqual(r.status_code, 400)
            self.assertFalse(verify_razorpay_signature(b"{}", "anything"))
        with patch.object(settings, "RAZORPAY_WEBHOOK_SECRET", "   "):
            self.assertFalse(verify_razorpay_signature(b"{}", "anything"))

    def test_whitespace_meta_secret_counts_as_unset(self):
        from app.services import meta_whatsapp_service as mws_mod

        with patch.object(settings, "META_APP_SECRET", "   "):
            self.assertFalse(mws_mod.verify_webhook_signature(b"body", "sha256=abc"))

    def test_razorpay_webhook_allowed_publicly(self):
        with patch.object(settings, "RAZORPAY_WEBHOOK_SECRET", ""), patch.object(settings, "ALLOW_UNSIGNED_WEBHOOKS", True):
            r = self.client.post("/api/payments/razorpay/webhook", json={"event": "ping"}, headers={"host": NGROK})
        self.assertEqual((r.status_code, r.json().get("status")), (200, "ignored"))

    def test_remote_peer_spoofing_localhost_host_header_blocked(self):
        # TestClient's peer "testclient" is not loopback once it is removed
        # from the allowed peers: this is a remote client sending
        # "Host: localhost" straight to the port (no proxy headers).
        with patch.object(settings, "LOCAL_PEER_ADDRESSES", "127.0.0.1,::1"):
            for host in ("localhost:8000", "127.0.0.1:8000", "[::1]:8000"):
                r = self.client.post("/api/generate-image", json={}, headers={"host": host})
                self.assertEqual(r.status_code, 404, host)
            self.assertEqual(
                self.client.get("/docs", headers={"host": "localhost"}).status_code, 404
            )
            with patch.object(settings, "ALLOW_UNSIGNED_WEBHOOKS", True), \
                 patch.object(settings, "RAZORPAY_WEBHOOK_SECRET", ""):
                r = self.client.post("/api/payments/razorpay/webhook", json={"event": "ping"},
                                     headers={"host": "localhost"})
            self.assertEqual(r.status_code, 200)

    def test_host_header_path_injection_rejected(self):
        # Starlette rebuilds request.url from Host: "x/api/meta/webhook#" used
        # to make the guard see a webhook path while the router served
        # /api/history to a remote peer.
        with patch.object(settings, "LOCAL_PEER_ADDRESSES", "127.0.0.1,::1"):
            for host in ("x/api/meta/webhook#", "x/api/meta/webhook?", "a b", "x@y", "h:po"):
                r = self.client.get("/api/history", headers={"host": host})
                self.assertEqual(r.status_code, 400, host)
            r = self.client.get("/api/history", headers={"host": NGROK})
            self.assertEqual(r.status_code, 404)

    def test_empty_or_duplicate_proxy_header_still_public(self):
        for headers in ([("host", "localhost:8000"), ("x-forwarded-for", "")],
                        [("host", "localhost:8000"), ("x-real-ip", "203.0.113.9")],
                        [("host", "localhost:8000"), ("FORWARDED", "for=203.0.113.9")]):
            r = self.client.get("/api/history", headers=headers)
            self.assertEqual(r.status_code, 404, headers)

    def test_oversized_public_webhook_body_refused(self):
        with patch.object(settings, "MAX_WEBHOOK_BODY_BYTES", 1000):
            r = self.client.post("/api/meta/webhook", content=b"{" + b" " * 2000 + b"}",
                                 headers={"host": NGROK, "content-type": "application/json"})
            self.assertEqual(r.status_code, 413)
            r = self.client.post("/api/meta/webhook", content=b"{}",
                                 headers={"host": NGROK, "content-type": "application/json"})
            self.assertNotEqual(r.status_code, 413)

    def test_peer_classification(self):
        from types import SimpleNamespace

        from app.middleware.public_host_guard import _is_local_peer

        def req(host):
            return SimpleNamespace(client=SimpleNamespace(host=host) if host is not None else None)

        with patch.object(settings, "LOCAL_PEER_ADDRESSES", "127.0.0.1,::1"):
            for peer in ("127.0.0.1", "127.0.0.5", "::1", "::ffff:127.0.0.1"):
                self.assertTrue(_is_local_peer(req(peer)), peer)
            for peer in ("203.0.113.5", "10.0.0.7", "::ffff:203.0.113.5", "testclient", "", None):
                self.assertFalse(_is_local_peer(req(peer)), peer)
        with patch.object(settings, "LOCAL_PEER_ADDRESSES", "192.168.1.20"):
            self.assertTrue(_is_local_peer(req("192.168.1.20")))

    def test_guard_can_be_disabled(self):
        with patch.object(settings, "PUBLIC_HOST_GUARD_ENABLED", False):
            r = self.client.get("/api/generate-image/health", headers={"host": NGROK})
        self.assertEqual(r.status_code, 200)


# ── 9. Signup guard ────────────────────────────────────────────────────────
class SignupGuardTests(unittest.TestCase):
    def test_signup_disabled_by_default(self):
        self.assertFalse(settings.ALLOW_SIGNUP)
        r = TestClient(app).post("/api/auth/signup", headers={"host": "localhost"},
                                 json={"email": "a@b.co", "username": "abc", "password": "Secret123!"})
        self.assertEqual(r.status_code, 403)

    def test_signup_enabled_reaches_service(self):
        with patch.object(settings, "ALLOW_SIGNUP", True), \
             patch("app.api.routes.auth.AuthService") as svc:
            svc.return_value.signup.side_effect = ValueError("exists")
            r = TestClient(app).post("/api/auth/signup", headers={"host": "localhost"},
                                     json={"email": "a@b.co", "username": "abc", "password": "Secret123!"})
        self.assertEqual(r.status_code, 409)  # existing behaviour of the route


# ── 7. OpenAI clients ──────────────────────────────────────────────────────
class OpenAIRetryTests(unittest.TestCase):
    def test_image_and_analysis_clients_use_max_retries_zero(self):
        from app.ai.providers.openai_image_provider import OpenAIImageProvider
        from app.ai.providers.openai_provider import OpenAIProvider

        seen = []

        def fake_client(**kwargs):
            seen.append(kwargs)
            raise RuntimeError("stop after construction")

        with patch("openai.AsyncOpenAI", side_effect=fake_client), \
             patch.object(settings, "OPENAI_API_KEY", "sk-test"):
            asyncio.run(OpenAIImageProvider().generate_image("p", {"request_id": "t"}))
            # A file in a temp directory, closed before use: re-opening a still-open
            # NamedTemporaryFile raises PermissionError on Windows, which made analyze()
            # return before it ever built the client this test is about.
            with tempfile.TemporaryDirectory() as tmp_dir:
                image_path = os.path.join(tmp_dir, "probe.jpg")
                with open(image_path, "wb") as f:
                    f.write(b"\xff\xd8\xff" + b"\x00" * 32)
                asyncio.run(OpenAIProvider().analyze([image_path], {"request_id": "t"}))
        self.assertEqual(len(seen), 2)
        self.assertTrue(all(k.get("max_retries") == 0 for k in seen), seen)


# ── 8/11/12. Fallback rules ────────────────────────────────────────────────
class FallbackTests(unittest.TestCase):
    def _run(self, gemini_result):
        from app.ai import image_generation_manager as igm
        from app.ai.providers.image_base import ImageGenerationResult

        gemini, openai = MagicMock(), MagicMock()
        gemini.is_available = openai.is_available = True
        gemini.supports_reference_image.return_value = openai.supports_reference_image.return_value = True
        gemini.generate_image = AsyncMock(return_value=gemini_result)
        openai.generate_image = AsyncMock(return_value=ImageGenerationResult(
            success=True, image_url="data:image/png;base64,QQ==", provider_name="openai"))
        mgr = igm.ImageGenerationManager()
        mgr._providers, mgr._initialised = {"gemini": gemini, "openai": openai}, True
        with patch.object(settings, "PRIMARY_IMAGE_PROVIDER", "gemini"), \
             patch.object(settings, "FALLBACK_IMAGE_PROVIDER", "openai"), \
             patch.object(settings, "GENERATION_ENABLED", True), \
             patch.object(settings, "MAX_GENERATIONS_PER_DAY", 100000):
            result = asyncio.run(mgr.generate_image("prompt", {"request_id": "t"}))
        return result, gemini, openai

    def _fail(self, error):
        from app.ai.providers.image_base import ImageGenerationResult
        return ImageGenerationResult(success=False, error=error, provider_name="gemini")

    def test_success_path_unchanged(self):
        from app.ai.providers.image_base import ImageGenerationResult
        ok = ImageGenerationResult(success=True, image_url="data:image/png;base64,Rw==", provider_name="gemini")
        result, gemini, openai = self._run(ok)
        self.assertTrue(result.success)
        self.assertEqual((result.provider_name, result.fallback_used), ("gemini", False))
        openai.generate_image.assert_not_awaited()

    def test_no_image_response_does_not_fall_back(self):
        result, _, openai = self._run(self._fail("Gemini returned a response but no image data was found."))
        self.assertFalse(result.success)
        openai.generate_image.assert_not_awaited()

    def test_genuine_gemini_failures_still_fall_back(self):
        for err in ("503 UNAVAILABLE. The model is overloaded", "Request timeout", "500 INTERNAL server error",
                    "404 NOT_FOUND. models/x is not found"):
            result, _, openai = self._run(self._fail(err))
            openai.generate_image.assert_awaited_once()
            self.assertTrue(result.success and result.fallback_used, err)

    def test_quota_still_halts(self):
        _, _, openai = self._run(self._fail("402 RESOURCE_EXHAUSTED. Your prepayment credits are depleted"))
        openai.generate_image.assert_not_awaited()


# ── 14. Pre-validation bounds ──────────────────────────────────────────────
class PrevalidationBoundsTests(unittest.TestCase):
    def _run(self, generate):
        from app.services import image_prevalidation_service as ips

        captured = {}

        class FakeClient:
            def __init__(self, **kwargs):
                captured["client"] = kwargs
                self.models = MagicMock()
                self.models.generate_content.side_effect = generate(captured)

        with patch("google.genai.Client", FakeClient), patch.object(settings, "GEMINI_API_KEY", "k"), \
             patch.object(settings, "IMAGE_PREVALIDATION_FAIL_OPEN", True):
            result = asyncio.run(ips.check_image_quality(b"\xff\xd8\xff\x00", "image/jpeg"))
        return result, captured

    def test_timeout_and_max_output_tokens_set(self):
        def generate(captured):
            def _gen(**kw):
                captured["config"] = kw["config"]
                raise RuntimeError("stop")
            return _gen
        _, captured = self._run(generate)
        self.assertEqual(captured["client"]["http_options"].timeout, 30_000)
        self.assertEqual(captured["config"].max_output_tokens, 2048)
        self.assertEqual(captured["config"].temperature, 0.0)

    def test_timeout_fails_open_without_retry(self):
        calls = []

        def generate(captured):
            def _gen(**kw):
                calls.append(1)
                raise TimeoutError("timed out")
            return _gen
        result, _ = self._run(generate)
        self.assertEqual(len(calls), 1)
        self.assertTrue(result.approved)
        self.assertFalse(result.checked)


# ── 3/5. Paid catalog failure + startup recovery ───────────────────────────
class PaidOrderSafetyTests(unittest.TestCase):
    def setUp(self):
        self.engine = make_engine()
        Base.metadata.create_all(bind=self.engine)
        self.Session = sessionmaker(bind=self.engine)
        self.db = self.Session()
        self.db.add(Customer(whatsapp_id=SENDER, full_name="T", business_name="B", gst_number="N/A",
                             address="N/A", wallet_balance=0, is_registered=True))
        self.tmp = tempfile.TemporaryDirectory()
        photo = Path(self.tmp.name) / "p.jpg"
        photo.write_bytes(b"\xff\xd8\xff" + b"\x00" * 32)
        self.image = Image(request_id="req-1", original_filename="p.jpg", stored_filename="p.jpg",
                           file_path=str(photo), file_size=35, mime_type="image/jpeg",
                           image_url="/uploads/req-1/p.jpg")
        self.db.add(self.image)
        self.db.commit()
        self.sent = AsyncMock(return_value=True)
        from app.ai import image_generation_manager as igm
        igm._spend_day, igm._spend_count = None, 0  # fresh daily spend counter
        self.patches = [patch("app.database.SessionLocal", self.Session),
                        patch.object(settings, "MAX_GENERATIONS_PER_DAY", 100000),
                        patch.object(mws, "send_whatsapp_text", self.sent),
                        patch.object(mws, "_check_failure_rate", lambda db: None)]
        for p in self.patches:
            p.start()

    def tearDown(self):
        for p in self.patches:
            p.stop()
        self.db.close()
        self.tmp.cleanup()

    def _order(self, status="pack_queued", product=PRODUCT_PACK_1, amount=500, age_minutes=0, mid="wamid.1"):
        row = WhatsAppIngestion(external_user_id=SENDER, external_message_id=mid, external_media_id="m",
                                channel="whatsapp", status=status, product_code=product,
                                amount_charged=amount, image_id=self.image.id, mime_type="image/jpeg")
        self.db.add(row)
        self.db.commit()
        if age_minutes:
            row.updated_at = datetime.now(timezone.utc) - timedelta(minutes=age_minutes)
            self.db.commit()
        return row.id

    def _state(self, ingestion_id):
        s = self.Session()
        try:
            row = s.get(WhatsAppIngestion, ingestion_id)
            bal = s.query(Customer.wallet_balance).filter(Customer.whatsapp_id == SENDER).scalar()
            refunds = s.query(AuditLog).filter(AuditLog.action == mws.REFUND_AUDIT_ACTION,
                                               AuditLog.resource_id == ingestion_id).count()
            return row.status, bal, refunds
        finally:
            s.close()

    def test_worker_exception_refunds_once_and_notifies(self):
        oid = self._order()
        with patch.object(mws, "_generate_single_pack_style", AsyncMock(return_value="data:image/png;base64,QQ==")), \
             patch.object(mws, "upload_media_to_meta", AsyncMock(side_effect=RuntimeError("boom"))):
            self.assertFalse(asyncio.run(mws.process_whatsapp_catalog_pack(oid)))
        self.assertEqual(self._state(oid), ("failed", 500, 1))
        text = self.sent.await_args.args[1]
        self.assertIn("Full Catalog Pack", text)
        self.assertIn("₹500 has been refunded", text)
        # A second failure (e.g. an admin retry) never refunds again.
        s = self.Session(); s.get(WhatsAppIngestion, oid).status = "stored"; s.commit(); s.close()
        with patch.object(mws, "_generate_single_pack_style", AsyncMock(return_value=None)):
            asyncio.run(mws.process_whatsapp_catalog_pack(oid))
        self.assertEqual(self._state(oid), ("failed", 500, 1))

    def test_generation_failure_notifies_with_refund(self):
        oid = self._order()
        with patch.object(mws, "_generate_single_pack_style", AsyncMock(return_value=None)):
            asyncio.run(mws.process_whatsapp_catalog_pack(oid))
        self.assertEqual(self._state(oid), ("failed", 500, 1))
        self.assertIn("₹500 has been refunded", self.sent.await_args.args[1])

    def test_success_path_unchanged(self):
        oid = self._order()
        with patch.object(mws, "_generate_single_pack_style", AsyncMock(return_value="data:image/png;base64,QQ==")), \
             patch.object(mws, "upload_media_to_meta", AsyncMock(return_value="media-1")), \
             patch.object(mws, "send_catalog_pack_images_to_whatsapp",
                          AsyncMock(side_effect=lambda **kw: len(kw["image_urls"]))):
            self.assertTrue(asyncio.run(mws.process_whatsapp_catalog_pack(oid)))
        self.assertEqual(self._state(oid), ("delivered", 0, 0))
        self.sent.assert_not_awaited()

    def test_startup_recovery_only_touches_stuck_paid_orders(self):
        stuck_pack = self._order("pack_queued", age_minutes=30, mid="w1")
        stuck_white = self._order("processing", PRODUCT_WHITE_BG, 50, age_minutes=30, mid="w2")
        recent = self._order("processing", age_minutes=2, mid="w3")
        done = self._order("delivered", age_minutes=60, mid="w4")
        legacy = self._order("processing", None, None, age_minutes=60, mid="w5")
        unpaid = self._order("white_queued", PRODUCT_WHITE_BG, 0, age_minutes=60, mid="w6")
        n = asyncio.run(mws.recover_stuck_paid_orders(timedelta(minutes=10)))
        self.assertEqual(n, 2)
        self.assertEqual(self._state(stuck_pack)[0::2], ("failed", 1))
        self.assertEqual(self._state(stuck_white)[0::2], ("failed", 1))
        self.assertEqual(self._state(stuck_white)[1], 550)
        for oid, status in ((recent, "processing"), (done, "delivered"), (legacy, "processing"),
                            (unpaid, "white_queued")):
            self.assertEqual(self._state(oid)[0::2], (status, 0))
        # Running it again refunds nothing more.
        self.assertEqual(asyncio.run(mws.recover_stuck_paid_orders(timedelta(minutes=10))), 0)
        self.assertEqual(self._state(stuck_pack)[1], 550)
        texts = [c.args[1] for c in self.sent.await_args_list]
        self.assertTrue(any("Clean Studio Shot" in t and "₹50 has been refunded" in t for t in texts))
        self.assertTrue(any("Full Catalog Pack" in t and "₹500 has been refunded" in t for t in texts))


if __name__ == "__main__":
    unittest.main()


class ChunkedBodyLimitTests(unittest.TestCase):
    """A chunked upload has no Content-Length, so the byte-counting limiter must stop it."""

    def setUp(self):
        self.client = TestClient(app)

    @staticmethod
    def _chunks(total, size=100):
        def gen():
            sent = 0
            while sent < total:
                piece = min(size, total - sent)
                sent += piece
                yield b" " * piece
        return gen()

    def test_oversized_chunked_webhook_body_gets_413(self):
        with patch.object(settings, "MAX_WEBHOOK_BODY_BYTES", 1000):
            for path in ("/api/meta/webhook", "/api/payments/razorpay/webhook"):
                r = self.client.post(path, content=self._chunks(5000),
                                     headers={"host": NGROK, "content-type": "application/json"})
                self.assertEqual(r.status_code, 413, path)

    def test_small_chunked_webhook_body_still_processed(self):
        with patch.object(settings, "MAX_WEBHOOK_BODY_BYTES", 1000), \
             patch.object(settings, "ALLOW_UNSIGNED_WEBHOOKS", True), \
             patch.object(settings, "RAZORPAY_WEBHOOK_SECRET", ""):
            def body():
                yield b'{"event":'
                yield b' "ping"}'
            r = self.client.post("/api/payments/razorpay/webhook", content=body(),
                                 headers={"host": NGROK, "content-type": "application/json"})
        self.assertEqual((r.status_code, r.json().get("status")), (200, "ignored"))

    def test_other_routes_are_not_limited_by_the_webhook_cap(self):
        with patch.object(settings, "MAX_WEBHOOK_BODY_BYTES", 10):
            r = self.client.post("/api/generate-image", json={"prompt": "x" * 500},
                                 headers={"host": "localhost:8000"})
        self.assertNotEqual(r.status_code, 413)
