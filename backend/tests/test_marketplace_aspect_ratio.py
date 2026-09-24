"""Tests for marketplace aspect-ratio alignment.

Verifies that:
1. Amazon marketplace overrides frontend aspect ratio to 1:1
2. Amazon marketplace still uses 1:1 if frontend sends another supported ratio
3. marketplace=None preserves existing aspect ratio
4. Unknown marketplace preserves existing aspect ratio and does not crash
5. Existing image-generation behavior remains unchanged otherwise
"""

import asyncio
import os
import sys
import unittest
from unittest.mock import MagicMock, patch

# Ensure the backend package is importable
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.ai.image_generation_manager import ImageGenerationManager
from app.ai.providers.image_base import ImageGenerationResult


def _make_mock_provider():
    """Create a mock provider that captures context."""
    provider = MagicMock()
    provider.is_available = True
    provider.supports_reference_image.return_value = False
    provider.provider_version = "1.0.0"
    provider.capabilities = ["image_generation"]
    return provider


class TestMarketplaceAspectRatio(unittest.TestCase):
    """Verify marketplace aspect-ratio override behavior."""

    @patch("app.ai.image_generation_manager.settings")
    def test_amazon_overrides_frontend_4_5_to_1_1(self, mock_settings):
        """Amazon marketplace overrides frontend aspect ratio 4:5 to 1:1."""
        mock_settings.PRIMARY_IMAGE_PROVIDER = "openai"
        mock_settings.FALLBACK_IMAGE_PROVIDER = "gemini"

        manager = ImageGenerationManager()
        manager._initialised = True

        captured_context = []

        mock_provider = _make_mock_provider()

        async def capture_generate(prompt, context, **kwargs):
            captured_context.append(dict(context))
            return ImageGenerationResult(
                success=True,
                image_url="data:image/png;base64,abc",
                image_data=b"fake",
                provider_name="openai",
                processing_time=1.0,
            )

        mock_provider.generate_image = capture_generate
        manager._providers = {"openai": mock_provider}

        result = asyncio.run(
            manager.generate_image(
                prompt="A gold earring",
                context={"aspect_ratio": "4:5", "request_id": "test-1"},
                marketplace="amazon_india_fashion_earrings",
            )
        )

        self.assertTrue(result.success)
        self.assertEqual(len(captured_context), 1)
        self.assertEqual(
            captured_context[0]["aspect_ratio"],
            "1:1",
            "Amazon marketplace must override 4:5 to 1:1",
        )

    @patch("app.ai.image_generation_manager.settings")
    def test_amazon_overrides_any_frontend_ratio(self, mock_settings):
        """Amazon marketplace overrides any frontend aspect ratio to 1:1."""
        mock_settings.PRIMARY_IMAGE_PROVIDER = "openai"
        mock_settings.FALLBACK_IMAGE_PROVIDER = "gemini"

        manager = ImageGenerationManager()
        manager._initialised = True

        captured_context = []

        mock_provider = _make_mock_provider()

        async def capture_generate(prompt, context, **kwargs):
            captured_context.append(dict(context))
            return ImageGenerationResult(
                success=True,
                image_url="data:image/png;base64,abc",
                image_data=b"fake",
                provider_name="openai",
                processing_time=1.0,
            )

        mock_provider.generate_image = capture_generate
        manager._providers = {"openai": mock_provider}

        result = asyncio.run(
            manager.generate_image(
                prompt="A gold earring",
                context={"aspect_ratio": "16:9", "request_id": "test-2"},
                marketplace="amazon_india_fashion_earrings",
            )
        )

        self.assertTrue(result.success)
        self.assertEqual(len(captured_context), 1)
        self.assertEqual(
            captured_context[0]["aspect_ratio"],
            "1:1",
            "Amazon marketplace must override 16:9 to 1:1",
        )

    @patch("app.ai.image_generation_manager.settings")
    def test_no_marketplace_preserves_aspect_ratio(self, mock_settings):
        """marketplace=None preserves the existing aspect ratio."""
        mock_settings.PRIMARY_IMAGE_PROVIDER = "openai"
        mock_settings.FALLBACK_IMAGE_PROVIDER = "gemini"

        manager = ImageGenerationManager()
        manager._initialised = True

        captured_context = []

        mock_provider = _make_mock_provider()

        async def capture_generate(prompt, context, **kwargs):
            captured_context.append(dict(context))
            return ImageGenerationResult(
                success=True,
                image_url="data:image/png;base64,abc",
                image_data=b"fake",
                provider_name="openai",
                processing_time=1.0,
            )

        mock_provider.generate_image = capture_generate
        manager._providers = {"openai": mock_provider}

        result = asyncio.run(
            manager.generate_image(
                prompt="A gold ring",
                context={"aspect_ratio": "4:5", "request_id": "test-3"},
                marketplace=None,
            )
        )

        self.assertTrue(result.success)
        self.assertEqual(len(captured_context), 1)
        self.assertEqual(
            captured_context[0]["aspect_ratio"],
            "4:5",
            "No marketplace must preserve the frontend aspect ratio",
        )

    @patch("app.ai.image_generation_manager.settings")
    def test_unknown_marketplace_preserves_aspect_ratio(self, mock_settings):
        """Unknown marketplace preserves the existing aspect ratio and does not crash."""
        mock_settings.PRIMARY_IMAGE_PROVIDER = "openai"
        mock_settings.FALLBACK_IMAGE_PROVIDER = "gemini"

        manager = ImageGenerationManager()
        manager._initialised = True

        captured_context = []

        mock_provider = _make_mock_provider()

        async def capture_generate(prompt, context, **kwargs):
            captured_context.append(dict(context))
            return ImageGenerationResult(
                success=True,
                image_url="data:image/png;base64,abc",
                image_data=b"fake",
                provider_name="openai",
                processing_time=1.0,
            )

        mock_provider.generate_image = capture_generate
        manager._providers = {"openai": mock_provider}

        result = asyncio.run(
            manager.generate_image(
                prompt="A gold ring",
                context={"aspect_ratio": "4:5", "request_id": "test-4"},
                marketplace="unknown_marketplace_xyz",
            )
        )

        self.assertTrue(result.success)
        self.assertEqual(len(captured_context), 1)
        self.assertEqual(
            captured_context[0]["aspect_ratio"],
            "4:5",
            "Unknown marketplace must preserve the frontend aspect ratio",
        )

    @patch("app.ai.image_generation_manager.settings")
    def test_marketplace_no_context_preserves_default(self, mock_settings):
        """Amazon marketplace with no context aspect_ratio sets the default."""
        mock_settings.PRIMARY_IMAGE_PROVIDER = "openai"
        mock_settings.FALLBACK_IMAGE_PROVIDER = "gemini"

        manager = ImageGenerationManager()
        manager._initialised = True

        captured_context = []

        mock_provider = _make_mock_provider()

        async def capture_generate(prompt, context, **kwargs):
            captured_context.append(dict(context))
            return ImageGenerationResult(
                success=True,
                image_url="data:image/png;base64,abc",
                image_data=b"fake",
                provider_name="openai",
                processing_time=1.0,
            )

        mock_provider.generate_image = capture_generate
        manager._providers = {"openai": mock_provider}

        result = asyncio.run(
            manager.generate_image(
                prompt="A gold earring",
                context={"request_id": "test-5"},
                marketplace="amazon_india_fashion_earrings",
            )
        )

        self.assertTrue(result.success)
        self.assertEqual(len(captured_context), 1)
        self.assertEqual(
            captured_context[0]["aspect_ratio"],
            "1:1",
            "Amazon marketplace must set aspect_ratio even when context has none",
        )

    @patch("app.ai.image_generation_manager.settings")
    def test_caller_context_not_mutated(self, mock_settings):
        """The caller's original context dict must not be mutated."""
        mock_settings.PRIMARY_IMAGE_PROVIDER = "openai"
        mock_settings.FALLBACK_IMAGE_PROVIDER = "gemini"

        manager = ImageGenerationManager()
        manager._initialised = True

        mock_provider = _make_mock_provider()

        async def ok_generate(prompt, context, **kwargs):
            return ImageGenerationResult(
                success=True,
                image_url="data:image/png;base64,abc",
                image_data=b"fake",
                provider_name="openai",
                processing_time=1.0,
            )

        mock_provider.generate_image = ok_generate
        manager._providers = {"openai": mock_provider}

        original_context = {"aspect_ratio": "4:5", "request_id": "test-6"}
        asyncio.run(
            manager.generate_image(
                prompt="A gold earring",
                context=original_context,
                marketplace="amazon_india_fashion_earrings",
            )
        )

        self.assertEqual(
            original_context["aspect_ratio"],
            "4:5",
            "Caller's context must not be mutated by the manager",
        )


if __name__ == "__main__":
    unittest.main()
