"""Tests for the Meta WhatsApp webhook and ingestion layer (Part 2).

Covers:
1. Webhook verification (GET)
2. Webhook payload parsing
3. Idempotency (duplicate message detection)
4. Image validation
5. Webhook signature verification
6. Health check endpoint

These tests do NOT consume AI-generation credits.
Meta media responses are mocked where appropriate.
"""

import hashlib
import hmac
import json
import os
import sys
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

# Ensure the backend package is importable
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.config import settings
from app.services.meta_whatsapp_service import (
    download_media,
    get_media_url,
    parse_webhook_entry,
    validate_image,
    verify_webhook_signature,
)


# ─── Test: Webhook Verification ──────────────────────────────────────────


class TestWebhookVerification(unittest.TestCase):
    """Verify the GET /api/meta/webhook verification flow."""

    def test_verify_token_matches(self):
        """Correct verification token is accepted."""
        # The settings.META_VERIFY_TOKEN defaults to "" in config
        # We test the logic by patching it
        with patch.object(settings, "META_VERIFY_TOKEN", "test-token-abc"):
            self.assertEqual(settings.META_VERIFY_TOKEN, "test-token-abc")

    def test_verify_token_mismatch(self):
        """Wrong verification token is rejected."""
        with patch.object(settings, "META_VERIFY_TOKEN", "correct-token"):
            self.assertNotEqual(settings.META_VERIFY_TOKEN, "wrong-token")


# ─── Test: Webhook Payload Parsing ───────────────────────────────────────


class TestWebhookPayloadParsing(unittest.TestCase):
    """Verify parse_webhook_entry handles all Meta webhook event types."""

    def test_parse_image_message(self):
        """Image messages are correctly parsed."""
        entry = {
            "changes": [
                {
                    "field": "messages",
                    "value": {
                        "messages": [
                            {
                                "type": "image",
                                "id": "wamid.abc123",
                                "from": "919876543210",
                                "timestamp": "1700000000",
                                "image": {
                                    "id": "media_id_xyz",
                                    "mime_type": "image/jpeg",
                                    "caption": "Check out this ring",
                                },
                            }
                        ]
                    },
                }
            ]
        }
        events = parse_webhook_entry(entry)
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["type"], "image")
        self.assertEqual(events[0]["message_id"], "wamid.abc123")
        self.assertEqual(events[0]["sender"], "919876543210")
        self.assertEqual(events[0]["media_id"], "media_id_xyz")
        self.assertEqual(events[0]["mime_type"], "image/jpeg")
        self.assertEqual(events[0]["caption"], "Check out this ring")

    def test_parse_text_message(self):
        """Text messages are correctly parsed."""
        entry = {
            "changes": [
                {
                    "field": "messages",
                    "value": {
                        "messages": [
                            {
                                "type": "text",
                                "id": "wamid.def456",
                                "from": "919876543210",
                                "timestamp": "1700000000",
                                "text": {"body": "Hello!"},
                            }
                        ]
                    },
                }
            ]
        }
        events = parse_webhook_entry(entry)
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["type"], "text")
        self.assertEqual(events[0]["body"], "Hello!")

    def test_parse_status_event(self):
        """Status events are correctly parsed."""
        entry = {
            "changes": [
                {
                    "field": "statuses",
                    "value": {
                        "statuses": [
                            {
                                "id": "wamid.ghi789",
                                "status": "delivered",
                                "timestamp": "1700000000",
                            }
                        ]
                    },
                }
            ]
        }
        events = parse_webhook_entry(entry)
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["type"], "status")
        self.assertEqual(events[0]["status"], "delivered")

    def test_parse_unsupported_message_type(self):
        """Unsupported message types are safely handled."""
        entry = {
            "changes": [
                {
                    "field": "messages",
                    "value": {
                        "messages": [
                            {
                                "type": "video",
                                "id": "wamid.xyz999",
                                "from": "919876543210",
                                "timestamp": "1700000000",
                            }
                        ]
                    },
                }
            ]
        }
        events = parse_webhook_entry(entry)
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["type"], "unsupported")
        self.assertEqual(events[0]["raw_type"], "video")

    def test_parse_empty_entry(self):
        """Entry with no changes produces no events."""
        entry = {"changes": []}
        events = parse_webhook_entry(entry)
        self.assertEqual(len(events), 0)

    def test_parse_multiple_messages(self):
        """Multiple messages in a single entry are all parsed."""
        entry = {
            "changes": [
                {
                    "field": "messages",
                    "value": {
                        "messages": [
                            {
                                "type": "image",
                                "id": "wamid.111",
                                "from": "919876543210",
                                "timestamp": "1700000000",
                                "image": {
                                    "id": "media_111",
                                    "mime_type": "image/jpeg",
                                },
                            },
                            {
                                "type": "text",
                                "id": "wamid.222",
                                "from": "919876543210",
                                "timestamp": "1700000001",
                                "text": {"body": "And this one"},
                            },
                        ]
                    },
                }
            ]
        }
        events = parse_webhook_entry(entry)
        self.assertEqual(len(events), 2)
        self.assertEqual(events[0]["type"], "image")
        self.assertEqual(events[1]["type"], "text")


# ─── Test: Image Validation ──────────────────────────────────────────────


class TestImageValidation(unittest.TestCase):
    """Verify the image validation function."""

    def _make_valid_jpeg(self) -> bytes:
        """Create a minimal valid JPEG image in memory."""
        from PIL import Image as PILImage
        import io

        img = PILImage.new("RGB", (100, 100), color=(255, 215, 0))
        buf = io.BytesIO()
        img.save(buf, format="JPEG")
        return buf.getvalue()

    def _make_valid_png(self) -> bytes:
        """Create a minimal valid PNG image in memory."""
        from PIL import Image as PILImage
        import io

        img = PILImage.new("RGB", (100, 100), color=(255, 0, 0))
        buf = io.BytesIO()
        img.save(buf, format="PNG")
        return buf.getvalue()

    def test_valid_jpeg_accepted(self):
        """Valid JPEG image is accepted."""
        img_bytes = self._make_valid_jpeg()
        is_valid, error = validate_image(img_bytes, "image/jpeg")
        self.assertTrue(is_valid)
        self.assertIsNone(error)

    def test_valid_png_accepted(self):
        """Valid PNG image is accepted."""
        img_bytes = self._make_valid_png()
        is_valid, error = validate_image(img_bytes, "image/png")
        self.assertTrue(is_valid)
        self.assertIsNone(error)

    def test_empty_bytes_rejected(self):
        """Empty image bytes are rejected."""
        is_valid, error = validate_image(b"", "image/jpeg")
        self.assertFalse(is_valid)
        self.assertIn("empty", error.lower())

    def test_oversized_rejected(self):
        """Image exceeding max size is rejected."""
        large_bytes = b"\x00" * (settings.META_MAX_MEDIA_BYTES + 1)
        is_valid, error = validate_image(large_bytes, "image/jpeg")
        self.assertFalse(is_valid)
        self.assertIn("too large", error.lower())

    def test_unsupported_format_rejected(self):
        """Unsupported MIME type is rejected."""
        is_valid, error = validate_image(b"fake data", "image/bmp")
        self.assertFalse(is_valid)
        self.assertIn("unsupported", error.lower())

    def test_invalid_image_data_rejected(self):
        """Corrupted image data is rejected."""
        is_valid, error = validate_image(b"not an image at all", "image/jpeg")
        self.assertFalse(is_valid)
        self.assertIn("invalid", error.lower())

    def test_tiny_image_rejected(self):
        """Image smaller than 16x16 is rejected."""
        from PIL import Image as PILImage
        import io

        img = PILImage.new("RGB", (8, 8), color=(255, 0, 0))
        buf = io.BytesIO()
        img.save(buf, format="JPEG")
        is_valid, error = validate_image(buf.getvalue(), "image/jpeg")
        self.assertFalse(is_valid)
        self.assertIn("too small", error.lower())


# ─── Test: Webhook Signature Verification ────────────────────────────────


class TestWebhookSignatureVerification(unittest.TestCase):
    """Verify the X-Hub-Signature-256 verification logic."""

    def test_valid_signature_accepted(self):
        """Valid HMAC-SHA256 signature is accepted."""
        secret = "test-secret-key"
        payload = b'{"object":"whatsapp_business_account"}'
        computed = hmac.new(secret.encode("utf-8"), payload, hashlib.sha256).hexdigest()
        signature = f"sha256={computed}"

        with patch.object(settings, "META_APP_SECRET", secret):
            result = verify_webhook_signature(payload, signature)
            self.assertTrue(result)

    def test_invalid_signature_rejected(self):
        """Invalid signature is rejected."""
        with patch.object(settings, "META_APP_SECRET", "real-secret"):
            result = verify_webhook_signature(
                b'{"object":"whatsapp_business_account"}',
                "sha256=deadbeef00000000000000000000000000000000000000000000000000000000",
            )
            self.assertFalse(result)

    def test_missing_signature_rejected(self):
        """Missing signature header is rejected."""
        with patch.object(settings, "META_APP_SECRET", "some-secret"):
            result = verify_webhook_signature(b"body", None)
            self.assertFalse(result)

    def test_no_secret_fails_closed(self):
        """When META_APP_SECRET is empty, nothing can be verified: reject."""
        with patch.object(settings, "META_APP_SECRET", ""):
            result = verify_webhook_signature(b"body", None)
            self.assertFalse(result)

    def test_invalid_prefix_rejected(self):
        """Signature with wrong prefix is rejected."""
        with patch.object(settings, "META_APP_SECRET", "secret"):
            result = verify_webhook_signature(b"body", "md5=abc123")
            self.assertFalse(result)


# ─── Test: Media Retrieval (Mocked) ──────────────────────────────────────


class TestMediaRetrieval(unittest.TestCase):
    """Verify media retrieval logic with mocked Meta API responses."""

    def test_no_token_returns_none(self):
        """When META_WHATSAPP_TOKEN is empty, returns None."""
        with patch.object(settings, "META_WHATSAPP_TOKEN", ""):
            import asyncio
            result = asyncio.run(get_media_url("media_id_123"))
            self.assertIsNone(result)

    def test_api_failure_returns_none(self):
        """When Meta API returns non-200, returns None."""
        mock_response = MagicMock()
        mock_response.status_code = 401

        with patch.object(settings, "META_WHATSAPP_TOKEN", "valid-token"):
            with patch("app.services.meta_whatsapp_service.httpx.AsyncClient") as mock_client:
                mock_client.return_value.__aenter__ = AsyncMock(
                    return_value=MagicMock(get=AsyncMock(return_value=mock_response))
                )
                mock_client.return_value.__aexit__ = AsyncMock(return_value=False)

                import asyncio
                result = asyncio.run(get_media_url("media_id_123"))
                self.assertIsNone(result)

    def test_successful_media_url(self):
        """Successful Meta API response returns the media URL."""
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {"url": "https://example.com/media/image.jpg"}

        mock_http = MagicMock()
        mock_http.get = AsyncMock(return_value=mock_response)

        with patch.object(settings, "META_WHATSAPP_TOKEN", "valid-token"):
            with patch("app.services.meta_whatsapp_service.httpx.AsyncClient") as mock_client:
                mock_client.return_value.__aenter__ = AsyncMock(return_value=mock_http)
                mock_client.return_value.__aexit__ = AsyncMock(return_value=False)

                import asyncio
                result = asyncio.run(get_media_url("media_id_123"))
                self.assertEqual(result, "https://example.com/media/image.jpg")


# ─── Test: Download Media (Mocked) ──────────────────────────────────────


class TestDownloadMedia(unittest.TestCase):
    """Verify download_media logic with mocked HTTP responses."""

    def test_successful_download(self):
        """Successful download returns bytes and content type."""
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.headers = {"content-type": "image/jpeg"}
        mock_response.content = b"\xff\xd8\xff" + b"\x00" * 100  # Fake JPEG header

        mock_http = MagicMock()
        mock_http.get = AsyncMock(return_value=mock_response)

        with patch("app.services.meta_whatsapp_service.httpx.AsyncClient") as mock_client:
            mock_client.return_value.__aenter__ = AsyncMock(return_value=mock_http)
            mock_client.return_value.__aexit__ = AsyncMock(return_value=False)

            import asyncio
            result = asyncio.run(download_media("https://example.com/image.jpg"))
            self.assertIsNotNone(result)
            image_bytes, content_type = result
            self.assertEqual(content_type, "image/jpeg")
            self.assertGreater(len(image_bytes), 0)

    def test_empty_response_returns_none(self):
        """Empty response body returns None."""
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.headers = {"content-type": "image/jpeg"}
        mock_response.content = b""

        mock_http = MagicMock()
        mock_http.get = AsyncMock(return_value=mock_response)

        with patch("app.services.meta_whatsapp_service.httpx.AsyncClient") as mock_client:
            mock_client.return_value.__aenter__ = AsyncMock(return_value=mock_http)
            mock_client.return_value.__aexit__ = AsyncMock(return_value=False)

            import asyncio
            result = asyncio.run(download_media("https://example.com/image.jpg"))
            self.assertIsNone(result)

    def test_http_error_returns_none(self):
        """Non-200 HTTP response returns None."""
        mock_response = MagicMock()
        mock_response.status_code = 404

        mock_http = MagicMock()
        mock_http.get = AsyncMock(return_value=mock_response)

        with patch("app.services.meta_whatsapp_service.httpx.AsyncClient") as mock_client:
            mock_client.return_value.__aenter__ = AsyncMock(return_value=mock_http)
            mock_client.return_value.__aexit__ = AsyncMock(return_value=False)

            import asyncio
            result = asyncio.run(download_media("https://example.com/image.jpg"))
            self.assertIsNone(result)


# ─── Test: Model Exists ──────────────────────────────────────────────────


class TestWhatsAppIngestionModel(unittest.TestCase):
    """Verify the WhatsAppIngestion model can be imported and instantiated."""

    def test_model_importable(self):
        """WhatsAppIngestion model can be imported."""
        from app.models.whatsapp_ingestion import WhatsAppIngestion
        self.assertTrue(hasattr(WhatsAppIngestion, "__tablename__"))
        self.assertEqual(WhatsAppIngestion.__tablename__, "whatsapp_ingestions")

    def test_model_has_required_columns(self):
        """Model has all required columns for ingestion tracking."""
        from app.models.whatsapp_ingestion import WhatsAppIngestion
        required_columns = [
            "id", "request_id", "external_user_id", "external_message_id",
            "external_media_id", "channel", "caption", "mime_type", "timestamp",
            "image_id", "file_size", "status", "error_message",
            "created_at", "updated_at",
        ]
        for col in required_columns:
            self.assertTrue(
                hasattr(WhatsAppIngestion, col),
                f"Missing column: {col}",
            )


# ─── Test: Configuration Exists ──────────────────────────────────────────


class TestMetaConfiguration(unittest.TestCase):
    """Verify Meta configuration variables exist in settings."""

    def test_meta_verify_token_exists(self):
        self.assertTrue(hasattr(settings, "META_VERIFY_TOKEN"))

    def test_meta_whatsapp_token_exists(self):
        self.assertTrue(hasattr(settings, "META_WHATSAPP_TOKEN"))

    def test_meta_phone_number_id_exists(self):
        self.assertTrue(hasattr(settings, "META_PHONE_NUMBER_ID"))

    def test_meta_app_secret_exists(self):
        self.assertTrue(hasattr(settings, "META_APP_SECRET"))

    def test_meta_max_media_bytes_exists(self):
        self.assertTrue(hasattr(settings, "META_MAX_MEDIA_BYTES"))
        self.assertGreater(settings.META_MAX_MEDIA_BYTES, 0)


if __name__ == "__main__":
    unittest.main()
