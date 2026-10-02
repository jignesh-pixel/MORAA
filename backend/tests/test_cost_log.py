"""COST-4: a row per provider call with an estimated cost, and a daily spend alert."""

import asyncio
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, patch

from app.ai import image_generation_manager as igm
from app.ai.providers.image_base import ImageGenerationResult
from app.config import settings
from app.database import SessionLocal, engine
from app.models.provider_call import ProviderCall
from app.services import alert_service, provider_call_log


class _Base(unittest.TestCase):
    def setUp(self):
        ProviderCall.__table__.create(bind=engine, checkfirst=True)
        self.addCleanup(lambda: ProviderCall.__table__.drop(bind=engine, checkfirst=True))
        provider_call_log.reset_for_tests()
        with SessionLocal() as db:
            db.query(ProviderCall).delete()
            db.commit()

    def rows(self):
        with SessionLocal() as db:
            return db.query(ProviderCall).order_by(ProviderCall.id).all()


class ErrorKindTests(unittest.TestCase):
    def test_categories(self):
        for text, kind in (("Image provider timeout: deadline exceeded", "timeout"), ("429 Too Many Requests", "rate_limit"),
                           ("quota per day exceeded", "quota"), ("prompt blocked by safety", "refused"),
                           ("weird", "other"), (None, None)):
            self.assertEqual(provider_call_log.error_kind(text), kind, text)

    def test_the_message_itself_is_never_stored(self):
        self.assertNotIn("secret", provider_call_log.error_kind("secret detail 429") or "")


class RecordTests(_Base):
    def test_a_success_costs_the_configured_price_and_a_failure_costs_nothing(self):
        with patch.object(settings, "COST_PER_CALL_GEMINI_RUPEES", 3.25):
            provider_call_log.record("gemini", "m1", True, 4.2, None, "req-1")
            provider_call_log.record("gemini", "m1", False, 1.0, "timeout", "req-2")
        ok, bad = self.rows()
        self.assertEqual((ok.outcome, ok.est_cost_paise, ok.latency_ms, ok.model), ("success", 325, 4200, "m1"))
        self.assertEqual((bad.outcome, bad.est_cost_paise, bad.error_kind), ("failure", 0, "timeout"))

    def test_unknown_price_is_counted_at_zero(self):
        provider_call_log.record("openai", None, True, 1.0, None, None)
        self.assertEqual(self.rows()[0].est_cost_paise, 0)

    def test_a_missing_table_is_silent_and_backs_off(self):
        ProviderCall.__table__.drop(bind=engine, checkfirst=True)
        provider_call_log.record("gemini", None, True, 1.0, None, None)          # must not raise
        ProviderCall.__table__.create(bind=engine, checkfirst=True)
        provider_call_log.record("gemini", None, True, 1.0, None, None)          # still backed off for a minute
        self.assertEqual(self.rows(), [])

    def test_summaries(self):
        with patch.object(settings, "COST_PER_CALL_GEMINI_RUPEES", 2.0):
            for _ in range(3):
                provider_call_log.record("gemini", None, True, 1.0, None, None)
        with SessionLocal() as db:
            self.assertEqual(provider_call_log.todays_summary(db), {"calls": 3, "rupees": 6})
            week = provider_call_log.days_summary(db)
        self.assertEqual((week[0]["provider"], week[0]["success"], week[0]["rupees"]), ("gemini", 3, 6))


class ManagerWritesTheLogTests(_Base):
    def test_every_provider_call_is_logged_with_its_outcome(self):
        provider = AsyncMock()
        provider.generate_image = AsyncMock(return_value=ImageGenerationResult(
            success=True, provider_name="gemini", model_used="gm", processing_time=2.5))
        m = igm.ImageGenerationManager()
        with patch.object(settings, "COST_PER_CALL_GEMINI_RUPEES", 1.5):
            asyncio.run(m._attempt_with_retries(provider, "gemini", "p", {}, None, "image/jpeg", "req-9"))
        row = self.rows()[0]
        self.assertEqual((row.provider, row.model, row.outcome, row.est_cost_paise, row.request_id),
                         ("gemini", "gm", "success", 150, "req-9"))

    def test_a_broken_log_never_breaks_generation(self):
        provider = AsyncMock()
        provider.generate_image = AsyncMock(return_value=ImageGenerationResult(success=True, provider_name="gemini"))
        m = igm.ImageGenerationManager()
        with patch.object(provider_call_log, "record", side_effect=RuntimeError("db down")):
            result = asyncio.run(m._attempt_with_retries(provider, "gemini", "p", {}, None, "image/jpeg", "r"))
        self.assertTrue(result.success)


class DailyCostAlertTests(_Base):
    def _alerts(self):
        with SessionLocal() as db:
            return [a.key for a in alert_service.evaluate_alerts(db)]

    def test_off_by_default(self):
        with patch.object(settings, "COST_PER_CALL_GEMINI_RUPEES", 50.0):
            provider_call_log.record("gemini", None, True, 1.0, None, None)
        self.assertNotIn("daily_cost", self._alerts())

    def test_alert_when_the_day_passes_the_line_and_not_before(self):
        with patch.object(settings, "OPS_ALERT_DAILY_COST_RUPEES", 100), patch.object(settings, "COST_PER_CALL_GEMINI_RUPEES", 40.0):
            provider_call_log.record("gemini", None, True, 1.0, None, None)
            provider_call_log.record("gemini", None, True, 1.0, None, None)
            self.assertNotIn("daily_cost", self._alerts())                       # Rs 80
            provider_call_log.record("gemini", None, True, 1.0, None, None)
            self.assertIn("daily_cost", self._alerts())                          # Rs 120

    def test_yesterdays_spend_does_not_count(self):
        with patch.object(settings, "OPS_ALERT_DAILY_COST_RUPEES", 10), patch.object(settings, "COST_PER_CALL_GEMINI_RUPEES", 40.0):
            provider_call_log.record("gemini", None, True, 1.0, None, None)
            with SessionLocal() as db:
                db.query(ProviderCall).update({"created_at": datetime.now(timezone.utc) - timedelta(days=2)})
                db.commit()
            self.assertNotIn("daily_cost", self._alerts())


if __name__ == "__main__":
    unittest.main()
