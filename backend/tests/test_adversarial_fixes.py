"""Phase 0: regressions for the issues the adversarial review confirmed."""

import hashlib
import hmac
import json
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient
from loguru import logger

from app.config import settings
from app.main import app
from app.middleware import public_host_guard as guard
from app.utils.logger import safe_log

NGROK = "abc123.ngrok-free.app"
META_SECRET = "meta-app-secret-for-tests"
RZP_SECRET = "rzp-webhook-secret-for-tests"


def _meta_sig(body: bytes) -> str:
    return "sha256=" + hmac.new(META_SECRET.encode(), body, hashlib.sha256).hexdigest()


class SignatureEdgeCaseTests(unittest.TestCase):
    def setUp(self):
        self.client = TestClient(app)
        self.patches = [
            patch.object(settings, "META_APP_SECRET", META_SECRET),
            patch.object(settings, "RAZORPAY_WEBHOOK_SECRET", RZP_SECRET),
            patch.object(settings, "ALLOW_UNSIGNED_WEBHOOKS", False),
        ]
        for p in self.patches:
            p.start()

    def tearDown(self):
        for p in reversed(self.patches):
            p.stop()

    def _meta(self, body: bytes, signature):
        headers = {"host": NGROK, "content-type": "application/json"}
        if signature is not None:
            headers["X-Hub-Signature-256"] = signature
        return self.client.post("/api/meta/webhook", content=body, headers=headers)

    def test_non_ascii_signature_headers_are_rejected_not_500(self):
        r = self._meta(b"{}", ("sha256=\u00e9\u00e9").encode("utf-8"))
        self.assertEqual((r.status_code, r.json()["message"]), (200, "Invalid signature"))
        r = self.client.post("/api/payments/razorpay/webhook", content=b"{}",
                             headers={"host": NGROK, "X-Razorpay-Signature": "\u00e9".encode("utf-8")})
        self.assertEqual(r.status_code, 400)

    def test_signed_non_object_json_is_ignored_not_500(self):
        for raw in (b"[]", b"123", b"null", b'"x"'):
            r = self._meta(raw, _meta_sig(raw))
            self.assertEqual(r.status_code, 200, raw)
            self.assertEqual(r.json()["status"], "ignored", raw)

    def test_unsigned_garbage_is_rejected_before_parsing(self):
        calls = []
        real_loads = json.loads

        def spy(*a, **k):
            calls.append(1)
            return real_loads(*a, **k)

        with patch("app.api.routes.meta_webhook.json.loads", side_effect=spy):
            r = self._meta(b"[" * 5000, None)
        self.assertEqual(r.json()["message"], "Invalid signature")
        self.assertEqual(calls, [])

    def test_valid_signature_still_processed(self):
        body = json.dumps({"object": "whatsapp_business_account", "entry": []}).encode()
        r = self._meta(body, _meta_sig(body))
        self.assertEqual(r.json()["status"], "ignored")
        self.assertIn("No entries", r.json()["message"])

    def test_verify_token_handshake(self):
        with patch.object(settings, "META_VERIFY_TOKEN", "verify-me-123"):
            ok = self.client.get("/api/meta/webhook", headers={"host": NGROK},
                                 params={"hub.mode": "subscribe", "hub.verify_token": "verify-me-123",
                                         "hub.challenge": "777"})
            bad = self.client.get("/api/meta/webhook", headers={"host": NGROK},
                                  params={"hub.mode": "subscribe", "hub.verify_token": "verify-me-124",
                                          "hub.challenge": "777"})
            odd = self.client.get("/api/meta/webhook", headers={"host": NGROK},
                                  params={"hub.mode": "subscribe", "hub.verify_token": "\u00e9",
                                          "hub.challenge": "777"})
        self.assertEqual((ok.status_code, ok.text), (200, "777"))
        self.assertEqual(bad.status_code, 403)
        self.assertEqual(odd.status_code, 403)


class LogHygieneTests(unittest.TestCase):
    def setUp(self):
        self.client = TestClient(app)
        self.records = []
        self.sink = logger.add(lambda m: self.records.append(m.record), level="DEBUG")

    def tearDown(self):
        logger.remove(self.sink)

    def test_safe_log_escapes_control_characters_and_caps_length(self):
        out = safe_log("/x\n2026-10-01 | INFO | FORGED\r\x00")
        self.assertNotIn("\n", out)
        self.assertNotIn("\r", out)
        self.assertNotIn("\x00", out)
        self.assertIn("\\x0a", out)
        self.assertEqual(len(safe_log("a" * 1000)), 303)

    def test_forged_log_line_in_path_cannot_start_a_new_line(self):
        forged = "/x%0a2026-10-01%2015:59:59.000%20|%20INFO%20|%20app.main:FORGED%20-%20Payment%20credited"
        with patch.object(settings, "LOCAL_PEER_ADDRESSES", "127.0.0.1,::1"):
            self.client.get(forged, headers={"host": NGROK})       # blocked by the guard
        self.client.get(forged, headers={"host": "localhost"})      # local: logged by the request middleware
        self.assertTrue(self.records)
        for record in self.records:
            self.assertNotIn("\n", record["message"])

    def test_blocked_request_logging_is_sampled(self):
        guard._blocked_window_start = 0.0
        guard._blocked_logged = guard._blocked_suppressed = 0
        with patch.object(settings, "LOCAL_PEER_ADDRESSES", "127.0.0.1,::1"):
            for i in range(150):
                self.client.get(f"/scan/{i}", headers={"host": NGROK})
        blocked = [r for r in self.records if "Public host guard: blocked" in r["message"]]
        self.assertLessEqual(len(blocked), guard._BLOCKED_LOG_PER_MINUTE)
        self.assertGreater(guard._blocked_suppressed, 100)


class LoginTimingTests(unittest.TestCase):
    def test_unknown_user_still_costs_a_password_check(self):
        from unittest.mock import MagicMock

        from app.services import auth_service as svc

        service = svc.AuthService.__new__(svc.AuthService)
        service.repo = MagicMock()
        service.repo.find_first.return_value = None
        with patch.object(svc, "verify_password", return_value=False) as check:
            with self.assertRaises(ValueError):
                service.login("nobody", "whatever")
        check.assert_called_once()
        self.assertEqual(check.call_args[0][1], svc._DUMMY_PASSWORD_HASH)


if __name__ == "__main__":
    unittest.main()
