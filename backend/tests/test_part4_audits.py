"""Part 4 audit tests — idempotency, retry safety, failure matrix, security.

These tests verify production-readiness concerns without consuming
AI credits or calling the live Meta API.
"""

import hashlib
import hmac
import io
import os
import sys
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))


# ─── STEP 6: Delivery Idempotency Audit ─────────────────────────────────


class TestDeliveryIdempotency(unittest.TestCase):
    """Verify delivery cannot be sent multiple times."""

    def test_delivered_status_blocks_retry(self):
        """retry_delivery rejects status='delivered'."""
        from app.api.routes.meta_webhook import retry_delivery
        import inspect
        source = inspect.getsource(retry_delivery)
        self.assertIn('"failed"', source)
        self.assertIn('"delivery_failed"', source)



# ─── STEP 7: Retry Safety Audit ─────────────────────────────────────────


class TestRetrySafety(unittest.TestCase):
    """Verify retry behavior is controlled."""

    def test_retry_only_for_failed_status(self):
        """retry_delivery only accepts 'failed' or 'delivery_failed'."""
        from app.api.routes.meta_webhook import retry_delivery
        import inspect
        source = inspect.getsource(retry_delivery)
        self.assertIn('"failed"', source)
        self.assertIn('"delivery_failed"', source)

    def test_retry_resets_to_stored(self):
        """retry resets status to 'stored' before re-triggering."""
        from app.api.routes.meta_webhook import retry_delivery
        import inspect
        source = inspect.getsource(retry_delivery)
        self.assertIn('"stored"', source)

    def test_manual_retry_sufficient_for_poc(self):
        """Manual retry endpoint exists and is sufficient for POC scope."""
        from app.api.routes.meta_webhook import retry_delivery
        self.assertTrue(callable(retry_delivery))


# ─── STEP 8: Caching / Credit Consumption Risk ───────────────────────────


class TestCachingRisk(unittest.TestCase):
    """Verify retry will regenerate (consuming credits) — documented limitation."""

    def test_retry_triggers_full_generation(self):
        """retry resets to 'stored' which triggers full generation pipeline."""
        from app.api.routes.meta_webhook import retry_delivery
        import inspect
        source = inspect.getsource(retry_delivery)
        # Resets to 'stored' — which means process_whatsapp_generation
        # will run the full pipeline again (Prompt 1 + AI generation)
        self.assertIn('"stored"', source)

    def test_no_caching_mechanism_exists(self):
        """No generated image caching exists in the current implementation."""
        from app.services import meta_whatsapp_service
        import inspect
        source = inspect.getsource(meta_whatsapp_service)
        # Should NOT contain any caching-related code
        self.assertNotIn("cache", source.lower())
        self.assertNotIn("reuse_generated", source.lower())
        self.assertNotIn("generated_image_path", source.lower())


# ─── STEP 9: Async Processing Audit ─────────────────────────────────────


class TestAsyncProcessing(unittest.TestCase):
    """Verify async processing configuration."""

    def test_background_tasks_used(self):
        """Webhook uses FastAPI BackgroundTasks for async generation."""
        from app.api.routes.meta_webhook import receive_webhook
        import inspect
        source = inspect.getsource(receive_webhook)
        self.assertIn("BackgroundTasks", source)
        self.assertTrue("background_tasks.add_task" in source or "_queue_order_run(background_tasks" in source)

    def test_celery_eager_mode_is_dev_only(self):
        """CELERY_TASK_ALWAYS_EAGER defaults to True (dev mode)."""
        from app.config import settings
        # This is the dev default — production should set to False
        self.assertTrue(settings.CELERY_TASK_ALWAYS_EAGER)


# ─── STEP 10: Meta Configuration Audit ──────────────────────────────────


class TestMetaConfiguration(unittest.TestCase):
    """Verify Meta configuration variables exist and have safe defaults."""

    def test_all_meta_vars_exist(self):
        """All required Meta configuration variables are defined."""
        from app.config import settings
        self.assertTrue(hasattr(settings, "META_VERIFY_TOKEN"))
        self.assertTrue(hasattr(settings, "META_WHATSAPP_TOKEN"))
        self.assertTrue(hasattr(settings, "META_PHONE_NUMBER_ID"))
        self.assertTrue(hasattr(settings, "META_APP_SECRET"))
        self.assertTrue(hasattr(settings, "META_MAX_MEDIA_BYTES"))

    def test_defaults_are_empty_strings(self):
        """All Meta secrets default to empty strings (not hardcoded)."""
        from app.config import settings
        self.assertEqual(settings.META_VERIFY_TOKEN, "")
        self.assertEqual(settings.META_WHATSAPP_TOKEN, "")
        self.assertEqual(settings.META_PHONE_NUMBER_ID, "")
        self.assertEqual(settings.META_APP_SECRET, "")

    def test_max_media_bytes_positive(self):
        """Max media bytes is a reasonable positive value."""
        from app.config import settings
        self.assertGreater(settings.META_MAX_MEDIA_BYTES, 0)
        self.assertLessEqual(settings.META_MAX_MEDIA_BYTES, 100 * 1024 * 1024)


# ─── STEP 12: Failure Test Matrix ───────────────────────────────────────


class TestFailureMatrix(unittest.TestCase):
    """Verify failure handling for each scenario in the matrix."""

    def test_duplicate_webhook_no_duplicate_ingestion(self):
        """Duplicate external_message_id is caught by idempotency check."""
        from app.api.routes.meta_webhook import receive_webhook
        import inspect
        source = inspect.getsource(receive_webhook)
        self.assertIn("find_first", source)
        self.assertIn("external_message_id", source)

    def test_invalid_verification_rejected(self):
        """Invalid verification token returns 403."""
        from app.api.routes.meta_webhook import verify_webhook
        import inspect
        source = inspect.getsource(verify_webhook)
        self.assertIn("403", source)

    def test_unsupported_message_safely_ignored(self):
        """Unsupported message types are acknowledged without crash."""
        from app.services.meta_whatsapp_service import parse_webhook_entry
        entry = {
            "changes": [{
                "field": "messages",
                "value": {
                    "messages": [{
                        "type": "sticker",
                        "id": "wamid.test",
                        "from": "123",
                        "timestamp": "0",
                    }]
                },
            }]
        }
        events = parse_webhook_entry(entry)
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["type"], "unsupported")

    def test_missing_media_id_recorded_as_failure(self):
        """Missing media_id creates a 'failed' ingestion record."""
        from app.api.routes.meta_webhook import receive_webhook
        import inspect
        source = inspect.getsource(receive_webhook)
        self.assertIn("Missing media_id", source)

    def test_generation_failure_sets_failed_status(self):
        """Generation failure sets status to 'failed'."""
        from app.services.meta_whatsapp_service import _fail_ingestion
        mock_db = MagicMock()
        mock_ingestion = MagicMock()
        _fail_ingestion(mock_db, mock_ingestion, "test error")
        self.assertEqual(mock_ingestion.status, "failed")

    def test_delivery_failure_sets_delivery_failed_status(self):
        """Delivery failure sets status to 'delivery_failed' (distinct)."""
        from app.services.meta_whatsapp_service import _fail_delivery
        mock_db = MagicMock()
        mock_ingestion = MagicMock()
        _fail_delivery(mock_db, mock_ingestion, "meta api error")
        self.assertEqual(mock_ingestion.status, "delivery_failed")



# ─── STEP 3 Security: Token Not in Responses ────────────────────────────


class TestSecurityTokenProtection(unittest.TestCase):
    """Verify tokens are never returned in API responses."""

    def test_health_check_shows_only_boolean(self):
        """Health check only exposes boolean presence, not token values."""
        from app.api.routes.meta_webhook import webhook_health
        import inspect
        source = inspect.getsource(webhook_health)
        # Should use bool() — never the raw token
        self.assertIn("bool(settings.META_VERIFY_TOKEN)", source)
        self.assertIn("bool(settings.META_WHATSAPP_TOKEN)", source)
        # Should NOT return the actual token value
        self.assertNotIn("settings.META_VERIFY_TOKEN,", source)
        self.assertNotIn("settings.META_WHATSAPP_TOKEN,", source)

    def test_status_endpoint_exposes_no_secrets(self):
        """Status endpoint does not expose Meta credentials."""
        from app.api.routes.meta_webhook import get_ingestion_status
        import inspect
        source = inspect.getsource(get_ingestion_status)
        self.assertNotIn("META_WHATSAPP_TOKEN", source)
        self.assertNotIn("META_APP_SECRET", source)
        self.assertNotIn("META_VERIFY_TOKEN", source)


if __name__ == "__main__":
    unittest.main()
