"""GET /api/meta/webhook must answer Meta's real (dotted) verification params."""

import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient

from app.config import settings
from app.main import app


class MetaVerificationTests(unittest.TestCase):
    def setUp(self):
        self.client = TestClient(app)
        self.patch = patch.object(settings, "META_VERIFY_TOKEN", "tok-123")
        self.patch.start()

    def tearDown(self):
        self.patch.stop()

    def _get(self, **params):
        return self.client.get("/api/meta/webhook", params=params)

    def test_meta_dotted_params_return_challenge(self):
        r = self._get(**{"hub.mode": "subscribe", "hub.verify_token": "tok-123", "hub.challenge": "1158201444"})
        self.assertEqual((r.status_code, r.text), (200, "1158201444"))

    def test_underscore_params_still_work(self):
        r = self._get(hub_mode="subscribe", hub_verify_token="tok-123", hub_challenge="42")
        self.assertEqual((r.status_code, r.text), (200, "42"))

    def test_wrong_token_or_mode_rejected(self):
        self.assertEqual(self._get(**{"hub.mode": "subscribe", "hub.verify_token": "nope", "hub.challenge": "1"}).status_code, 403)
        self.assertEqual(self._get(**{"hub.mode": "unsubscribe", "hub.verify_token": "tok-123", "hub.challenge": "1"}).status_code, 403)


if __name__ == "__main__":
    unittest.main()
