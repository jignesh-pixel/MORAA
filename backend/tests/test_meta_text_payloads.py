"""Meta WhatsApp payload structure for plain text and reply-button CTA messages.

Kept from the retired onboarding_service test module: these exercise the live
``app.services.meta_whatsapp_service`` helpers. The HTTP client is mocked, so
no real WhatsApp API call is made.
"""

import asyncio
import os
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.config import settings

SENDER = "919876543210"
RECHARGE_BUTTON_ID = "recharge_500"
RECHARGE_BUTTON_TITLE = "💳 Recharge to use"


class TestMetaPayloads(unittest.TestCase):
    """The onboarding messages use the existing Meta WhatsApp helpers."""

    def _post_payload(self, coro_fn, *args):
        captured = {}

        class _Response:
            status_code = 200

            @staticmethod
            def json():
                return {"messages": [{"id": "wamid.test"}]}

        class _Client:
            def __init__(self, *a, **kw):
                pass

            async def __aenter__(self):
                return self

            async def __aexit__(self, *exc):
                return False

            async def post(self, url, headers=None, json=None):
                captured["payload"] = json
                return _Response()

        with patch(
            "app.services.meta_whatsapp_service.httpx.AsyncClient", new=_Client
        ), patch.object(settings, "META_WHATSAPP_TOKEN", "test-token"), patch.object(
            settings, "META_PHONE_NUMBER_ID", "123456"
        ):
            ok = asyncio.run(coro_fn(*args))

        return ok, captured.get("payload")

    def test_text_message_payload(self):
        from app.services.meta_whatsapp_service import send_text_message

        ok, payload = self._post_payload(send_text_message, SENDER, "Hello")

        self.assertTrue(ok)
        self.assertEqual(payload["messaging_product"], "whatsapp")
        self.assertEqual(payload["to"], SENDER)
        self.assertEqual(payload["type"], "text")
        self.assertEqual(payload["text"]["body"], "Hello")

    def test_recharge_cta_payload(self):
        from app.services.meta_whatsapp_service import send_interactive_cta_button

        ok, payload = self._post_payload(
            send_interactive_cta_button,
            SENDER,
            "Body text",
            RECHARGE_BUTTON_ID,
            RECHARGE_BUTTON_TITLE,
        )

        self.assertTrue(ok)
        self.assertEqual(payload["type"], "interactive")
        interactive = payload["interactive"]
        self.assertEqual(interactive["type"], "button")
        self.assertEqual(interactive["body"]["text"], "Body text")
        button = interactive["action"]["buttons"][0]
        self.assertEqual(button["type"], "reply")
        self.assertEqual(button["reply"]["id"], "recharge_500")
        self.assertEqual(button["reply"]["title"], "💳 Recharge to use")


if __name__ == "__main__":
    unittest.main()
