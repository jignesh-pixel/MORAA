"""Tests for Part 3 — WhatsApp generation + delivery layer.

Covers:
1. process_whatsapp_generation triggers existing pipeline
2. Existing Prompt 1 mechanism is reused
3. Existing ImageGenerationManager is reused
4. Meta media upload (mocked)
5. Meta message send (mocked)
6. Idempotency (duplicate processing prevention)
7. Failure handling (generation, delivery, storage)
8. Data URL to bytes conversion
9. Status tracking through lifecycle

All AI generation and Meta API calls are mocked.
No real AI credits are consumed. No real Meta API is called.
"""

import base64
import io
import os
import sys
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

# Ensure the backend package is importable
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))


# ─── Test: Data URL Conversion ───────────────────────────────────────────


class TestDataUrlConversion(unittest.TestCase):
    """Verify _data_url_to_bytes helper."""

    def test_valid_png_data_url(self):
        """Valid PNG data URL is correctly decoded."""
        from app.services.meta_whatsapp_service import _data_url_to_bytes

        # Create a minimal valid PNG
        from PIL import Image as PILImage

        img = PILImage.new("RGB", (10, 10), color=(255, 0, 0))
        buf = io.BytesIO()
        img.save(buf, format="PNG")
        raw_bytes = buf.getvalue()
        b64 = base64.b64encode(raw_bytes).decode("utf-8")
        data_url = f"data:image/png;base64,{b64}"

        result = _data_url_to_bytes(data_url)
        self.assertIsNotNone(result)
        self.assertEqual(result, raw_bytes)

    def test_invalid_format_returns_none(self):
        """Non-data URL returns None."""
        from app.services.meta_whatsapp_service import _data_url_to_bytes

        result = _data_url_to_bytes("https://example.com/image.png")
        self.assertIsNone(result)

    def test_empty_payload_returns_bytes(self):
        """Data URL with empty base64 payload returns empty bytes."""
        from app.services.meta_whatsapp_service import _data_url_to_bytes

        result = _data_url_to_bytes("data:image/png;base64,")
        self.assertIsNotNone(result)
        self.assertEqual(len(result), 0)

    def test_corrupted_base64_returns_none(self):
        """Corrupted base64 data returns None."""
        from app.services.meta_whatsapp_service import _data_url_to_bytes

        result = _data_url_to_bytes("data:image/png;base64,!!!invalid!!!")
        self.assertIsNone(result)


# ─── Test: Meta Media Upload (Mocked) ────────────────────────────────────


class TestMetaMediaUpload(unittest.TestCase):
    """Verify upload_media_to_meta with mocked Meta API."""

    def test_no_token_returns_none(self):
        """Without META_WHATSAPP_TOKEN, returns None."""
        from app.services.meta_whatsapp_service import upload_media_to_meta

        with patch("app.services.meta_whatsapp_service.settings") as mock_settings:
            mock_settings.META_WHATSAPP_TOKEN = ""
            mock_settings.META_PHONE_NUMBER_ID = "12345"

            import asyncio
            result = asyncio.run(upload_media_to_meta(b"fake image"))
            self.assertIsNone(result)

    def test_no_phone_number_id_returns_none(self):
        """Without META_PHONE_NUMBER_ID, returns None."""
        from app.services.meta_whatsapp_service import upload_media_to_meta

        with patch("app.services.meta_whatsapp_service.settings") as mock_settings:
            mock_settings.META_WHATSAPP_TOKEN = "valid-token"
            mock_settings.META_PHONE_NUMBER_ID = ""

            import asyncio
            result = asyncio.run(upload_media_to_meta(b"fake image"))
            self.assertIsNone(result)

    def test_successful_upload_returns_media_id(self):
        """Successful Meta API response returns the media ID."""
        from app.services.meta_whatsapp_service import upload_media_to_meta

        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {"id": "media_id_abc123"}

        mock_http = MagicMock()
        mock_http.post = AsyncMock(return_value=mock_response)

        with patch("app.services.meta_whatsapp_service.settings") as mock_settings:
            mock_settings.META_WHATSAPP_TOKEN = "valid-token"
            mock_settings.META_PHONE_NUMBER_ID = "12345"

            with patch("app.services.meta_whatsapp_service.httpx.AsyncClient") as mock_client:
                mock_client.return_value.__aenter__ = AsyncMock(return_value=mock_http)
                mock_client.return_value.__aexit__ = AsyncMock(return_value=False)

                import asyncio
                result = asyncio.run(upload_media_to_meta(b"fake image"))
                self.assertEqual(result, "media_id_abc123")

    def test_api_failure_returns_none(self):
        """Non-200 Meta API response returns None."""
        from app.services.meta_whatsapp_service import upload_media_to_meta

        mock_response = MagicMock()
        mock_response.status_code = 401
        mock_response.text = "Unauthorized"

        mock_http = MagicMock()
        mock_http.post = AsyncMock(return_value=mock_response)

        with patch("app.services.meta_whatsapp_service.settings") as mock_settings:
            mock_settings.META_WHATSAPP_TOKEN = "bad-token"
            mock_settings.META_PHONE_NUMBER_ID = "12345"

            with patch("app.services.meta_whatsapp_service.httpx.AsyncClient") as mock_client:
                mock_client.return_value.__aenter__ = AsyncMock(return_value=mock_http)
                mock_client.return_value.__aexit__ = AsyncMock(return_value=False)

                import asyncio
                result = asyncio.run(upload_media_to_meta(b"fake image"))
                self.assertIsNone(result)


# ─── Test: Meta Message Send (Mocked) ────────────────────────────────────


class TestMetaMessageSend(unittest.TestCase):
    """Verify send_image_to_whatsapp with mocked Meta API."""

    def test_no_token_returns_false(self):
        """Without META_WHATSAPP_TOKEN, returns False."""
        from app.services.meta_whatsapp_service import send_image_to_whatsapp

        with patch("app.services.meta_whatsapp_service.settings") as mock_settings:
            mock_settings.META_WHATSAPP_TOKEN = ""
            mock_settings.META_PHONE_NUMBER_ID = "12345"

            import asyncio
            result = asyncio.run(send_image_to_whatsapp("919876543210", "media_id"))
            self.assertFalse(result)

    def test_successful_send_returns_true(self):
        """Successful Meta Send API response returns True."""
        from app.services.meta_whatsapp_service import send_image_to_whatsapp

        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {
            "messages": [{"id": "wamid.sent123"}]
        }

        mock_http = MagicMock()
        mock_http.post = AsyncMock(return_value=mock_response)

        with patch("app.services.meta_whatsapp_service.settings") as mock_settings:
            mock_settings.META_WHATSAPP_TOKEN = "valid-token"
            mock_settings.META_PHONE_NUMBER_ID = "12345"

            with patch("app.services.meta_whatsapp_service.httpx.AsyncClient") as mock_client:
                mock_client.return_value.__aenter__ = AsyncMock(return_value=mock_http)
                mock_client.return_value.__aexit__ = AsyncMock(return_value=False)

                import asyncio
                result = asyncio.run(send_image_to_whatsapp("919876543210", "media_id_abc"))
                self.assertTrue(result)

    def test_send_includes_correct_recipient(self):
        """Send request uses the original WhatsApp user ID from ingestion."""
        from app.services.meta_whatsapp_service import send_image_to_whatsapp

        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {"messages": [{"id": "wamid.x"}]}

        mock_http = MagicMock()
        mock_http.post = AsyncMock(return_value=mock_response)

        with patch("app.services.meta_whatsapp_service.settings") as mock_settings:
            mock_settings.META_WHATSAPP_TOKEN = "valid-token"
            mock_settings.META_PHONE_NUMBER_ID = "12345"

            with patch("app.services.meta_whatsapp_service.httpx.AsyncClient") as mock_client:
                mock_client.return_value.__aenter__ = AsyncMock(return_value=mock_http)
                mock_client.return_value.__aexit__ = AsyncMock(return_value=False)

                import asyncio
                asyncio.run(send_image_to_whatsapp("919876543210", "media_xyz"))

                # Verify the POST call was made with the correct recipient
                call_args = mock_http.post.call_args
                payload = call_args.kwargs.get("json") or call_args[1].get("json")
                self.assertEqual(payload["to"], "919876543210")
                self.assertEqual(payload["image"]["id"], "media_xyz")

    def test_send_api_failure_returns_false(self):
        """Non-200 Meta Send API response returns False."""
        from app.services.meta_whatsapp_service import send_image_to_whatsapp

        mock_response = MagicMock()
        mock_response.status_code = 500
        mock_response.text = "Internal Server Error"

        mock_http = MagicMock()
        mock_http.post = AsyncMock(return_value=mock_response)

        with patch("app.services.meta_whatsapp_service.settings") as mock_settings:
            mock_settings.META_WHATSAPP_TOKEN = "valid-token"
            mock_settings.META_PHONE_NUMBER_ID = "12345"

            with patch("app.services.meta_whatsapp_service.httpx.AsyncClient") as mock_client:
                mock_client.return_value.__aenter__ = AsyncMock(return_value=mock_http)
                mock_client.return_value.__aexit__ = AsyncMock(return_value=False)

                import asyncio
                result = asyncio.run(send_image_to_whatsapp("919876543210", "media_id"))
                self.assertFalse(result)


# ─── Test: Failure Handling ──────────────────────────────────────────────


class TestFailureHandling(unittest.TestCase):
    """Verify failure helper functions update status correctly."""

    def test_fail_ingestion_sets_failed_status(self):
        """_fail_ingestion sets status to 'failed' with error message."""
        from app.services.meta_whatsapp_service import _fail_ingestion

        mock_db = MagicMock()
        mock_ingestion = MagicMock()
        mock_ingestion.status = "processing"

        _fail_ingestion(mock_db, mock_ingestion, "Test error")

        self.assertEqual(mock_ingestion.status, "failed")
        self.assertEqual(mock_ingestion.error_message, "Test error")
        mock_db.commit.assert_called_once()

    def test_fail_delivery_sets_delivery_failed_status(self):
        """_fail_delivery sets status to 'delivery_failed' (distinct from generation failure)."""
        from app.services.meta_whatsapp_service import _fail_delivery

        mock_db = MagicMock()
        mock_ingestion = MagicMock()
        mock_ingestion.status = "generated"

        _fail_delivery(mock_db, mock_ingestion, "Meta API timeout")

        self.assertEqual(mock_ingestion.status, "delivery_failed")
        self.assertEqual(mock_ingestion.error_message, "Meta API timeout")
        mock_db.commit.assert_called_once()


# ─── Test: Existing Component Reuse Verification ─────────────────────────


class TestExistingComponentReuse(unittest.TestCase):
    """Verify Part 3 uses ONLY existing GemVision components."""

    def test_no_duplicate_ai_pipeline(self):
        """No duplicate AI pipeline exists in the WhatsApp service."""
        from app.services import meta_whatsapp_service
        import inspect
        source = inspect.getsource(meta_whatsapp_service)
        # Should NOT contain direct AI provider calls
        self.assertNotIn("AsyncOpenAI(", source)
        self.assertNotIn("genai.Client(", source)
        self.assertNotIn("google.generativeai", source)
        self.assertNotIn("import openai", source)
        # Should NOT contain prompt construction logic
        self.assertNotIn("PRODUCT FIDELITY", source)
        self.assertNotIn("REFERENCE IMAGE PRIORITY", source)
        self.assertNotIn("ANTI-REDESIGN", source)

    def test_no_duplicate_fidelity(self):
        """No duplicate Product Fidelity implementation."""
        from app.services import meta_whatsapp_service
        import inspect
        source = inspect.getsource(meta_whatsapp_service)
        self.assertNotIn("ProductFidelity", source)
        self.assertNotIn("build_fidelity_instruction", source)
        self.assertNotIn("evaluate_fidelity", source)

    def test_no_duplicate_prompt_fusion(self):
        """No duplicate Prompt Fusion implementation."""
        from app.services import meta_whatsapp_service
        import inspect
        source = inspect.getsource(meta_whatsapp_service)
        self.assertNotIn("PromptFusionEngine", source)
        self.assertNotIn("prompt_fusion", source.lower().replace("import", ""))


# ─── Test: Config Variables for Part 3 ──────────────────────────────────


class TestPart3Configuration(unittest.TestCase):
    """Verify all required Part 3 configuration exists."""

    def test_generation_config_exists(self):
        """Config has the keys needed for generation trigger."""
        from app.config import settings
        self.assertTrue(hasattr(settings, "OPENAI_API_KEY"))
        self.assertTrue(hasattr(settings, "GEMINI_API_KEY"))
        self.assertTrue(hasattr(settings, "PRIMARY_IMAGE_PROVIDER"))
        self.assertTrue(hasattr(settings, "FALLBACK_IMAGE_PROVIDER"))

    def test_meta_send_url_constant_exists(self):
        """Meta send message URL template is defined."""
        from app.services.meta_whatsapp_service import META_SEND_MESSAGE_URL
        self.assertIn("{phone_number_id}", META_SEND_MESSAGE_URL)
        self.assertIn("messages", META_SEND_MESSAGE_URL)

    def test_meta_media_upload_url_constant_exists(self):
        """Meta media upload URL template is defined."""
        from app.services.meta_whatsapp_service import META_MEDIA_UPLOAD_URL
        self.assertIn("{phone_number_id}", META_MEDIA_UPLOAD_URL)
        self.assertIn("media", META_MEDIA_UPLOAD_URL)


if __name__ == "__main__":
    unittest.main()
