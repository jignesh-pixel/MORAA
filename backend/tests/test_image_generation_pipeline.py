"""Tests for the image generation pipeline.

Verifies:
1. Provider chain order — OpenAI (primary) → Gemini (fallback)
2. Reference priority block appended when reference image present
3. Fallback logic — recoverable errors fall back; non-recoverable errors and
   quota/billing exhaustion halt the chain (no fallback, no retries)
4. Provider chain respects config overrides
5. _is_recoverable_error / _is_non_recoverable_error / _is_quota_exhaustion
   classification
"""

import asyncio
import os
import sys
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

# Ensure the backend package is importable
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.ai.image_generation_manager import (
    REFERENCE_PRIORITY_BLOCK,
    ImageGenerationManager,
    _is_quota_exhaustion,
    _is_non_recoverable_error,
    _is_recoverable_error,
)
from app.ai.providers.image_base import ImageGenerationResult


# ─── Test: Provider chain order ────────────────────────────────────────


class TestProviderChainOrder(unittest.TestCase):
    """Verify the provider chain defaults to OpenAI (primary) → Gemini (fallback)."""

    @patch("app.ai.image_generation_manager.settings")
    def test_default_chain_is_openai_then_gemini(self, mock_settings):
        """When config values are their defaults, chain is [openai, gemini]."""
        mock_settings.PRIMARY_IMAGE_PROVIDER = "openai"
        mock_settings.FALLBACK_IMAGE_PROVIDER = "gemini"

        manager = ImageGenerationManager()
        chain = manager._get_provider_chain()

        self.assertEqual(chain, ["openai", "gemini"])

    @patch("app.ai.image_generation_manager.settings")
    def test_empty_config_falls_back_to_openai_primary(self, mock_settings):
        """When PRIMARY_IMAGE_PROVIDER is empty string, default is 'openai'."""
        mock_settings.PRIMARY_IMAGE_PROVIDER = ""
        mock_settings.FALLBACK_IMAGE_PROVIDER = ""

        manager = ImageGenerationManager()
        chain = manager._get_provider_chain()

        self.assertEqual(chain[0], "openai", "Primary must be openai, not gemini")
        self.assertEqual(chain[1], "gemini", "Fallback must be gemini, not openai")

    @patch("app.ai.image_generation_manager.settings")
    def test_none_config_falls_back_to_openai_primary(self, mock_settings):
        """When PRIMARY_IMAGE_PROVIDER is None, default is 'openai'."""
        mock_settings.PRIMARY_IMAGE_PROVIDER = None
        mock_settings.FALLBACK_IMAGE_PROVIDER = None

        manager = ImageGenerationManager()
        chain = manager._get_provider_chain()

        self.assertEqual(chain[0], "openai")
        self.assertEqual(chain[1], "gemini")

    @patch("app.ai.image_generation_manager.settings")
    def test_config_override_gemini_primary(self, mock_settings):
        """User can override to make Gemini primary via config."""
        mock_settings.PRIMARY_IMAGE_PROVIDER = "gemini"
        mock_settings.FALLBACK_IMAGE_PROVIDER = "openai"

        manager = ImageGenerationManager()
        chain = manager._get_provider_chain()

        self.assertEqual(chain, ["gemini", "openai"])

    @patch("app.ai.image_generation_manager.settings")
    def test_duplicate_provider_deduplication(self, mock_settings):
        """If primary and fallback are the same, chain has only one entry."""
        mock_settings.PRIMARY_IMAGE_PROVIDER = "openai"
        mock_settings.FALLBACK_IMAGE_PROVIDER = "openai"

        manager = ImageGenerationManager()
        chain = manager._get_provider_chain()

        self.assertEqual(chain, ["openai"])

    @patch("app.ai.image_generation_manager.settings")
    def test_unknown_provider_excluded(self, mock_settings):
        """Unknown provider names are excluded from the chain."""
        mock_settings.PRIMARY_IMAGE_PROVIDER = "nonexistent_model"
        mock_settings.FALLBACK_IMAGE_PROVIDER = "openai"

        manager = ImageGenerationManager()
        chain = manager._get_provider_chain()

        self.assertNotIn("nonexistent_model", chain)
        self.assertIn("openai", chain)


# ─── Test: Reference priority block ───────────────────────────────────


class TestReferencePriorityBlock(unittest.TestCase):
    """Verify the reference priority block is appended correctly."""

    @patch("app.ai.image_generation_manager.settings")
    def test_reference_block_appended_when_reference_present(self, mock_settings):
        """REFERENCE_PRIORITY_BLOCK is appended when reference_image is provided."""
        mock_settings.PRIMARY_IMAGE_PROVIDER = "openai"
        mock_settings.FALLBACK_IMAGE_PROVIDER = "gemini"

        manager = ImageGenerationManager()
        manager._initialised = True

        # Mock provider that returns success
        mock_provider = MagicMock()
        mock_provider.is_available = True
        mock_provider.supports_reference_image.return_value = True

        captured_prompt = []

        async def capture_generate(prompt, context, **kwargs):
            captured_prompt.append(prompt)
            return ImageGenerationResult(
                success=True,
                image_url="data:image/png;base64,abc",
                image_data=b"fake",
                provider_name="openai",
                processing_time=1.0,
            )

        mock_provider.generate_image = capture_generate
        manager._providers = {"openai": mock_provider}

        # Call with reference image
        result = asyncio.run(
            manager.generate_image(
                prompt="A gold ring on marble",
                reference_image=b"fake_image_bytes",
            )
        )

        self.assertTrue(result.success)
        self.assertEqual(len(captured_prompt), 1)
        self.assertIn("REFERENCE IMAGE PRIORITY", captured_prompt[0])

    @patch("app.ai.image_generation_manager.settings")
    def test_reference_block_not_appended_without_reference(self, mock_settings):
        """REFERENCE_PRIORITY_BLOCK is NOT appended when no reference_image."""
        mock_settings.PRIMARY_IMAGE_PROVIDER = "openai"
        mock_settings.FALLBACK_IMAGE_PROVIDER = "gemini"

        manager = ImageGenerationManager()
        manager._initialised = True

        captured_prompt = []

        mock_provider = MagicMock()
        mock_provider.is_available = True
        mock_provider.supports_reference_image.return_value = False

        async def capture_generate(prompt, context, **kwargs):
            captured_prompt.append(prompt)
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
            manager.generate_image(prompt="A gold ring on marble")
        )

        self.assertTrue(result.success)
        self.assertEqual(len(captured_prompt), 1)
        self.assertNotIn("REFERENCE IMAGE PRIORITY", captured_prompt[0])

    @patch("app.ai.image_generation_manager.settings")
    def test_reference_block_not_duplicated_when_already_present(self, mock_settings):
        """If prompt already contains the reference block, it's not added again."""
        mock_settings.PRIMARY_IMAGE_PROVIDER = "openai"
        mock_settings.FALLBACK_IMAGE_PROVIDER = "gemini"

        manager = ImageGenerationManager()
        manager._initialised = True

        captured_prompt = []

        mock_provider = MagicMock()
        mock_provider.is_available = True
        mock_provider.supports_reference_image.return_value = True

        async def capture_generate(prompt, context, **kwargs):
            captured_prompt.append(prompt)
            return ImageGenerationResult(
                success=True,
                image_url="data:image/png;base64,abc",
                image_data=b"fake",
                provider_name="openai",
                processing_time=1.0,
            )

        mock_provider.generate_image = capture_generate
        manager._providers = {"openai": mock_provider}

        prompt_with_ref = f"A gold ring. {REFERENCE_PRIORITY_BLOCK}"
        result = asyncio.run(
            manager.generate_image(
                prompt=prompt_with_ref,
                reference_image=b"fake_image_bytes",
            )
        )

        self.assertTrue(result.success)
        # Should not be doubled
        count = captured_prompt[0].count("REFERENCE IMAGE PRIORITY")
        self.assertEqual(count, 1, "Reference block must not be duplicated")


# ─── Test: Error classification ────────────────────────────────────────


class TestErrorClassification(unittest.TestCase):
    """Verify recoverable / non-recoverable / quota-halt classification."""

    def test_recoverable_429(self):
        self.assertTrue(_is_recoverable_error("Error 429: Too many requests"))

    def test_quota_exhaustion_is_not_recoverable(self):
        """Quota exhaustion is a project-level halt, never a recoverable error."""
        self.assertFalse(_is_recoverable_error("Quota exceeded for project"))
        self.assertTrue(_is_quota_exhaustion("Quota exceeded for project"))

    def test_recoverable_timeout(self):
        self.assertTrue(_is_recoverable_error("Request timeout after 60s"))

    def test_recoverable_503(self):
        self.assertTrue(_is_recoverable_error("503 Service Unavailable"))

    def test_resource_exhausted_is_not_recoverable(self):
        """RESOURCE_EXHAUSTED / billing exhaustion halts and is never recoverable."""
        self.assertFalse(_is_recoverable_error("RESOURCE_EXHAUSTED: billing limit"))
        self.assertTrue(_is_quota_exhaustion("RESOURCE_EXHAUSTED: billing limit"))

    def test_non_recoverable_safety(self):
        self.assertTrue(_is_non_recoverable_error("Content blocked by safety filter"))

    def test_non_recoverable_bad_request(self):
        self.assertTrue(_is_non_recoverable_error("400 Bad Request: invalid payload"))

    def test_non_recoverable_api_key(self):
        self.assertTrue(_is_non_recoverable_error("API key is invalid"))

    def test_non_recoverable_prompt_blocked(self):
        self.assertTrue(_is_non_recoverable_error("Prompt was blocked due to harmful content"))

    def test_empty_string_not_recoverable(self):
        self.assertFalse(_is_recoverable_error(""))

    def test_empty_string_not_non_recoverable(self):
        self.assertFalse(_is_non_recoverable_error(""))

    def test_normal_error_not_classified(self):
        """A generic error that doesn't match any pattern."""
        self.assertFalse(_is_recoverable_error("Something went wrong"))
        self.assertFalse(_is_non_recoverable_error("Something went wrong"))


# ─── Test: Fallback behaviour ──────────────────────────────────────────


class TestFallbackBehaviour(unittest.TestCase):
    """Verify fallback triggers on recoverable errors, not on non-recoverable."""

    @patch("app.ai.image_generation_manager.settings")
    def test_fallback_on_recoverable_error(self, mock_settings):
        """When the primary returns a recoverable error, the fallback is tried."""
        mock_settings.PRIMARY_IMAGE_PROVIDER = "openai"
        mock_settings.FALLBACK_IMAGE_PROVIDER = "gemini"

        manager = ImageGenerationManager()
        manager._initialised = True

        # Primary fails with recoverable error
        primary_provider = MagicMock()
        primary_provider.is_available = True
        primary_provider.supports_reference_image.return_value = False

        async def primary_fail(prompt, context, **kwargs):
            return ImageGenerationResult(
                success=False,
                error="429 Too many requests",
                provider_name="openai",
                processing_time=0.5,
            )

        primary_provider.generate_image = primary_fail

        # Fallback succeeds
        fallback_provider = MagicMock()
        fallback_provider.is_available = True
        fallback_provider.supports_reference_image.return_value = False

        async def fallback_ok(prompt, context, **kwargs):
            return ImageGenerationResult(
                success=True,
                image_url="data:image/png;base64,abc",
                image_data=b"fake",
                provider_name="gemini",
                processing_time=2.0,
            )

        fallback_provider.generate_image = fallback_ok

        manager._providers = {
            "openai": primary_provider,
            "gemini": fallback_provider,
        }

        result = asyncio.run(
            manager.generate_image(prompt="A gold ring")
        )

        self.assertTrue(result.success)
        self.assertEqual(result.provider_name, "gemini")
        self.assertTrue(result.fallback_used)

    @patch("app.ai.image_generation_manager.settings")
    def test_no_fallback_on_non_recoverable_error(self, mock_settings):
        """When the primary returns a non-recoverable error, fallback is NOT tried."""
        mock_settings.PRIMARY_IMAGE_PROVIDER = "openai"
        mock_settings.FALLBACK_IMAGE_PROVIDER = "gemini"

        manager = ImageGenerationManager()
        manager._initialised = True

        # Primary fails with non-recoverable error
        primary_provider = MagicMock()
        primary_provider.is_available = True
        primary_provider.supports_reference_image.return_value = False

        async def primary_blocked(prompt, context, **kwargs):
            return ImageGenerationResult(
                success=False,
                error="Content blocked by safety filter",
                provider_name="openai",
                processing_time=0.3,
            )

        primary_provider.generate_image = primary_blocked

        # Fallback should NOT be called
        fallback_provider = MagicMock()
        fallback_provider.is_available = True
        fallback_provider.supports_reference_image.return_value = False

        fallback_called = []

        async def fallback_track(prompt, context, **kwargs):
            fallback_called.append(True)
            return ImageGenerationResult(
                success=True,
                image_url="data:image/png;base64,abc",
                image_data=b"fake",
                provider_name="gemini",
                processing_time=2.0,
            )

        fallback_provider.generate_image = fallback_track

        manager._providers = {
            "openai": primary_provider,
            "gemini": fallback_provider,
        }

        result = asyncio.run(
            manager.generate_image(prompt="A gold ring")
        )

        self.assertFalse(result.success)
        self.assertEqual(result.provider_name, "openai")
        self.assertEqual(len(fallback_called), 0, "Fallback must NOT be called for non-recoverable errors")

    @patch("app.ai.image_generation_manager.settings")
    def test_quota_exhaustion_halts_and_does_not_retry(self, mock_settings):
        """Quota exhaustion halts after ONE attempt: no retry, no fallback.

        Under the non-recoverable quota policy, a 429 / resource-exhausted
        response is a project-level condition. The chain must stop instead of
        consuming the fallback provider's quota on the same request.
        """
        mock_settings.PRIMARY_IMAGE_PROVIDER = "openai"
        mock_settings.FALLBACK_IMAGE_PROVIDER = "gemini"

        manager = ImageGenerationManager()
        manager._initialised = True

        primary_calls = []

        primary_provider = MagicMock()
        primary_provider.is_available = True
        primary_provider.supports_reference_image.return_value = False

        async def primary_quota(prompt, context, **kwargs):
            primary_calls.append(True)
            return ImageGenerationResult(
                success=False,
                error="429 Resource exhausted: quota exceeded for this project",
                provider_name="openai",
                processing_time=0.2,
            )

        primary_provider.generate_image = primary_quota

        fallback_calls = []

        fallback_provider = MagicMock()
        fallback_provider.is_available = True
        fallback_provider.supports_reference_image.return_value = False

        async def fallback_track(prompt, context, **kwargs):
            fallback_calls.append(True)
            return ImageGenerationResult(
                success=True,
                image_url="data:image/png;base64,abc",
                image_data=b"fake",
                provider_name="gemini",
                processing_time=2.0,
            )

        fallback_provider.generate_image = fallback_track

        manager._providers = {
            "openai": primary_provider,
            "gemini": fallback_provider,
        }

        result = asyncio.run(manager.generate_image(prompt="A gold ring"))

        self.assertFalse(result.success)
        self.assertEqual(result.provider_name, "openai")
        self.assertTrue(result.metadata.get("non_recoverable"))
        self.assertEqual(len(primary_calls), 1, "Quota exhaustion must attempt exactly once")
        self.assertEqual(len(fallback_calls), 0, "Quota exhaustion must never trigger the fallback")
        self.assertEqual(
            ImageGenerationManager.MAX_RETRIES_PER_PROVIDER,
            0,
            "Provider retries are disabled by the 0-retry policy",
        )


if __name__ == "__main__":
    unittest.main()
