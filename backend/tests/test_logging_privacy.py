"""Phase 0 / U5 (OBS-1, OBS-5, PRIV-1): request logs reach the API log, and
customer data stays out of the logs."""

import unittest
from unittest.mock import AsyncMock, patch

from fastapi.testclient import TestClient
from loguru import logger

from app.config import settings
from app.main import app
from app.utils.logger import mask_phone

SENDER = "919812345678"


class _Capture:
    def __init__(self):
        self.records = []

    def __enter__(self):
        self.sink_id = logger.add(lambda m: self.records.append(m.record), level="DEBUG")
        return self

    def __exit__(self, *exc):
        logger.remove(self.sink_id)

    def messages(self, category=None):
        return [r["message"] for r in self.records
                if category is None or r["extra"].get("category") == category]


class RequestLogTests(unittest.TestCase):
    def setUp(self):
        self.client = TestClient(app)

    def test_request_lines_carry_the_api_category(self):
        # The api_*.log sink filters on extra["category"] == "api"; with the
        # old extra={...} kwarg it was nested one level down and never matched.
        with _Capture() as cap:
            self.client.get("/health", headers={"host": "localhost"})
        api_lines = cap.messages("api")
        self.assertTrue(any("GET /health" in line and "→ 200" in line for line in api_lines), api_lines)

    def test_braces_in_path_do_not_break_logging(self):
        with _Capture() as cap:
            r = self.client.get("/api/%7Bx%7D/{0}", headers={"host": "localhost"})
        self.assertEqual(r.status_code, 404)
        self.assertTrue(any("{x}" in line for line in cap.messages("api")))

    def test_query_string_secrets_not_logged(self):
        with _Capture() as cap:
            self.client.get("/api/meta/webhook?hub.mode=subscribe&hub.verify_token=SECRET-TOKEN-123",
                            headers={"host": "localhost"})
        self.assertFalse(any("SECRET-TOKEN-123" in m for m in cap.messages()))


class MaskPhoneTests(unittest.TestCase):
    def test_masking(self):
        self.assertEqual(mask_phone("919812345678"), "91******5678")
        self.assertEqual(mask_phone("+14155550123"), "+1******0123")
        self.assertEqual(mask_phone("1234"), "****")
        self.assertEqual(mask_phone(None), "")


class InboundTextPrivacyTests(unittest.TestCase):
    def test_message_body_and_full_phone_never_logged(self):
        body = "Name: Ananya Shah, GSTIN 24AAAPS1234C1Z5, 12 Diamond Plaza Surat"
        payload = {"object": "whatsapp_business_account", "entry": [{"changes": [{"field": "messages",
                   "value": {"messages": [{"type": "text", "id": "wamid.priv.1", "from": SENDER,
                                           "timestamp": "1", "text": {"body": body}}]}}]}]}
        with _Capture() as cap, \
             patch.object(settings, "META_APP_SECRET", ""), \
             patch("app.api.routes.meta_webhook.send_whatsapp_text", new=AsyncMock(return_value=True)), \
             patch("app.api.routes.meta_webhook.send_registration_flow", new=AsyncMock(return_value=True),
                   create=True):
            TestClient(app).post("/api/meta/webhook", json=payload, headers={"host": "localhost"})
        everything = "\n".join(cap.messages())
        self.assertIn("Text message received", everything)
        for secret in ("24AAAPS1234C1Z5", "Diamond Plaza", "Ananya"):
            self.assertNotIn(secret, everything)
        received = [m for m in cap.messages() if "Text message received" in m]
        self.assertTrue(all(SENDER not in m for m in received), received)


if __name__ == "__main__":
    unittest.main()
