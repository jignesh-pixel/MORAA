"""Tests for OpenAI reference-image identity anchor.

Verifies:
1. Identity anchor is included when reference image is present
2. Identity anchor is NOT included when reference image is absent
3. Existing prompt content is preserved after anchor is added
4. Reference image bytes are passed unchanged
"""

import asyncio
import os
import sys
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

# Ensure the backend package is importable
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.ai.providers.openai_image_provider import (
    OPENAI_IDENTITY_ANCHOR,
    OpenAIImageProvider,
)
from app.ai.providers.image_base import ImageGenerationResult


class TestOpenAIIdentityAnchor(unittest.TestCase):
    """Verify the OpenAI identity anchor behavior."""

    def test_anchor_constant_exists(self):
        """OPENAI_IDENTITY_ANCHOR is defined and non-empty."""
        self.assertIsInstance(OPENAI_IDENTITY_ANCHOR, str)
        self.assertGreater(len(OPENAI_IDENTITY_ANCHOR), 50)

    def test_anchor_contains_identity_protection(self):
        """Anchor contains key identity-protection phrases."""
        self.assertIn("authoritative source", OPENAI_IDENTITY_ANCHOR)
        self.assertIn("Preserve the exact product", OPENAI_IDENTITY_ANCHOR)
        self.assertIn("Do not redesign", OPENAI_IDENTITY_ANCHOR)
        self.assertIn("Only apply the requested scene", OPENAI_IDENTITY_ANCHOR)

    def test_anchor_does_not_make_product_claims(self):
        """Anchor does not claim specific stone count, metal colour, etc."""
        self.assertNotIn("gold", OPENAI_IDENTITY_ANCHOR.lower())
        self.assertNotIn("diamond", OPENAI_IDENTITY_ANCHOR.lower())
        self.assertNotIn("earring", OPENAI_IDENTITY_ANCHOR.lower())
        self.assertNotIn("ring", OPENAI_IDENTITY_ANCHOR.lower())
        self.assertNotIn("1:", OPENAI_IDENTITY_ANCHOR)  # no aspect ratio
        self.assertNotIn("white background", OPENAI_IDENTITY_ANCHOR.lower())

    @patch("app.ai.providers.openai_image_provider.settings")
    def test_anchor_prepended_with_reference_image(self, mock_settings):
        """When reference image is present, identity anchor is prepended to prompt."""
        mock_settings.OPENAI_API_KEY = "test-key"
        mock_settings.OPENAI_IMAGE_MODEL = "gpt-image-1"

        provider = OpenAIImageProvider()

        # Mock the OpenAI client
        mock_response = MagicMock()
        mock_response.data = [MagicMock()]
        mock_response.data[0].b64_json = "dGVzdA=="  # base64 "test"
        mock_response.data[0].url = None

        captured_prompt = []

        mock_client = MagicMock()
        mock_edit = AsyncMock(return_value=mock_response)

        async def capture_edit(**kwargs):
            captured_prompt.append(kwargs.get("prompt", ""))
            return mock_response

        mock_client.images.edit = capture_edit

        with patch("openai.AsyncOpenAI", return_value=mock_client):
            asyncio.run(
                provider.generate_image(
                    prompt="A gold ring on marble",
                    context={"request_id": "test", "aspect_ratio": "1:1"},
                    reference_image=b"fake_image_bytes",
                    reference_mime_type="image/jpeg",
                )
            )

        self.assertEqual(len(captured_prompt), 1)
        prompt = captured_prompt[0]

        # Anchor must be present
        self.assertIn("authoritative source", prompt)
        self.assertIn("Preserve the exact product", prompt)

        # Original prompt must also be present
        self.assertIn("A gold ring on marble", prompt)

        # Anchor must come before the original prompt
        anchor_pos = prompt.index("authoritative source")
        original_pos = prompt.index("A gold ring on marble")
        self.assertLess(anchor_pos, original_pos, "Anchor must precede the original prompt")

    @patch("app.ai.providers.openai_image_provider.settings")
    def test_no_anchor_without_reference_image(self, mock_settings):
        """When reference image is absent, identity anchor is NOT added."""
        mock_settings.OPENAI_API_KEY = "test-key"
        mock_settings.OPENAI_IMAGE_MODEL = "gpt-image-1"

        provider = OpenAIImageProvider()

        mock_response = MagicMock()
        mock_response.data = [MagicMock()]
        mock_response.data[0].b64_json = "dGVzdA=="
        mock_response.data[0].url = None

        captured_prompt = []

        mock_client = MagicMock()

        async def capture_generate(**kwargs):
            captured_prompt.append(kwargs.get("prompt", ""))
            return mock_response

        mock_client.images.generate = capture_generate

        with patch("openai.AsyncOpenAI", return_value=mock_client):
            asyncio.run(
                provider.generate_image(
                    prompt="A gold ring on marble",
                    context={"request_id": "test", "aspect_ratio": "1:1"},
                    reference_image=None,
                )
            )

        self.assertEqual(len(captured_prompt), 1)
        prompt = captured_prompt[0]

        # Anchor must NOT be present
        self.assertNotIn("authoritative source", prompt)
        self.assertNotIn("Preserve the exact product", prompt)

        # Original prompt must be unchanged
        self.assertEqual(prompt, "A gold ring on marble")

    @patch("app.ai.providers.openai_image_provider.settings")
    def test_existing_prompt_fully_preserved(self, mock_settings):
        """Prompt containing scene + REFERENCE_PRIORITY_BLOCK + Amazon marketplace is fully preserved."""
        mock_settings.OPENAI_API_KEY = "test-key"
        mock_settings.OPENAI_IMAGE_MODEL = "gpt-image-1"

        provider = OpenAIImageProvider()

        mock_response = MagicMock()
        mock_response.data = [MagicMock()]
        mock_response.data[0].b64_json = "dGVzdA=="
        mock_response.data[0].url = None

        captured_prompt = []

        mock_client = MagicMock()

        async def capture_edit(**kwargs):
            captured_prompt.append(kwargs.get("prompt", ""))
            return mock_response

        mock_client.images.edit = capture_edit

        # Simulate a realistic prompt from the pipeline
        full_prompt = (
            "Professional product photography. 18K Gold Diamond Ring.\n\n"
            "REFERENCE IMAGE PRIORITY: MAXIMUM\n"
            "The uploaded image is the primary product.\n"
            "Preserve every visible detail exactly.\n\n"
            "AMAZON INDIA — MAIN PRODUCT IMAGE (PRESENTATION ONLY):\n"
            "Pure white background — RGB 255, 255, 255."
        )

        with patch("openai.AsyncOpenAI", return_value=mock_client):
            asyncio.run(
                provider.generate_image(
                    prompt=full_prompt,
                    context={"request_id": "test", "aspect_ratio": "1:1"},
                    reference_image=b"fake_image_bytes",
                    reference_mime_type="image/jpeg",
                )
            )

        self.assertEqual(len(captured_prompt), 1)
        prompt = captured_prompt[0]

        # All existing content must be present
        self.assertIn("Professional product photography", prompt)
        self.assertIn("REFERENCE IMAGE PRIORITY: MAXIMUM", prompt)
        self.assertIn("AMAZON INDIA — MAIN PRODUCT IMAGE", prompt)
        self.assertIn("Pure white background", prompt)

        # Anchor must also be present
        self.assertIn("authoritative source", prompt)

    @patch("app.ai.providers.openai_image_provider.settings")
    def test_reference_bytes_passed_unchanged(self, mock_settings):
        """Reference image bytes are passed to the temp file unchanged."""
        mock_settings.OPENAI_API_KEY = "test-key"
        mock_settings.OPENAI_IMAGE_MODEL = "gpt-image-1"

        provider = OpenAIImageProvider()

        mock_response = MagicMock()
        mock_response.data = [MagicMock()]
        mock_response.data[0].b64_json = "dGVzdA=="
        mock_response.data[0].url = None

        written_bytes = []

        mock_client = MagicMock()

        async def capture_edit(**kwargs):
            return mock_response

        mock_edit = MagicMock(side_effect=capture_edit)
        mock_client.images.edit = mock_edit

        reference_bytes = b"EXACT_REFERENCE_IMAGE_BYTES_12345"

        with patch("openai.AsyncOpenAI", return_value=mock_client):
            asyncio.run(
                provider.generate_image(
                    prompt="A test prompt",
                    context={"request_id": "test", "aspect_ratio": "1:1"},
                    reference_image=reference_bytes,
                    reference_mime_type="image/png",
                )
            )

        # Verify images.edit was called (meaning the reference image path was entered)
        mock_edit.assert_called_once()

    @patch("app.ai.providers.openai_image_provider.settings")
    def test_no_reference_no_image_editing_call(self, mock_settings):
        """Without reference image, images.generate is called (not images.edit)."""
        mock_settings.OPENAI_API_KEY = "test-key"
        mock_settings.OPENAI_IMAGE_MODEL = "gpt-image-1"

        provider = OpenAIImageProvider()

        mock_response = MagicMock()
        mock_response.data = [MagicMock()]
        mock_response.data[0].b64_json = "dGVzdA=="
        mock_response.data[0].url = None

        mock_client = MagicMock()
        mock_client.images.generate = AsyncMock(return_value=mock_response)

        with patch("openai.AsyncOpenAI", return_value=mock_client):
            asyncio.run(
                provider.generate_image(
                    prompt="A test prompt",
                    context={"request_id": "test", "aspect_ratio": "1:1"},
                    reference_image=None,
                )
            )

        # images.generate should be called (text-to-image mode)
        mock_client.images.generate.assert_called_once()
        # images.edit should NOT be called
        mock_client.images.edit.assert_not_called()


if __name__ == "__main__":
    unittest.main()
