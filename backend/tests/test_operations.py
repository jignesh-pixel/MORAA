"""Phase 4 operations: health checks (OBS-2), metrics (OBS-3), error-report scrubbing (OBS-3), WhatsApp alerts (OBS-4)."""

import asyncio
import json
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, patch

from fastapi import Response
from sqlalchemy.orm import sessionmaker

import app.models  # noqa: F401 -- register every model with Base.metadata
from app.api.routes import health
from app.config import settings
from app.database import Base
from app.models.audit_log import AuditLog
from app.models.pending_payment import PendingPayment
from app.models.whatsapp_ingestion import WhatsAppIngestion
from app.services import alert_service, metrics, spend_counter
from app.services.error_tracking import scrub_event
from tests.db_support import make_engine


class HealthTests(unittest.TestCase):
    def test_live_never_touches_anything(self):
        self.assertEqual(asyncio.run(health.health_live()).status, "healthy")
        self.assertEqual(asyncio.run(health.health_check()).status, "healthy")

    def ready(self, db=(True, "ok"), schema=(True, "ok"), disk=(True, "ok")):
        response = Response()
        with patch.object(health, "_check_database", return_value=db), \
             patch.object(health, "_check_schema", return_value=schema), \
             patch.object(health, "_check_disk", return_value=disk):
            body = health.health_ready(response)
        return response.status_code, body

    def test_ready_when_every_check_passes(self):
        code, body = self.ready()
        self.assertEqual((code, body["status"]), (200, "ready"))

    def test_not_ready_with_503_when_any_check_fails(self):
        for failing in ({"db": (False, "database unreachable")}, {"schema": (False, "behind")}, {"disk": (False, "read-only")}):
            code, body = self.ready(**failing)
            self.assertEqual((code, body["status"]), (503, "not_ready"), failing)
        _, body = self.ready(schema=(False, "behind: database 0008, code 0013"))
        self.assertEqual(body["checks"]["schema"], {"ok": False, "detail": "behind: database 0008, code 0013"})

    def test_the_real_disk_and_database_checks_run(self):
        engine = make_engine(concurrent=True)
        self.addCleanup(engine.dispose)
        with patch("app.database.engine", engine):
            ok, detail = health._check_database()
        self.assertTrue(ok, detail)
        self.assertTrue(health._check_disk()[0])

    def test_a_dead_database_is_reported_not_raised(self):
        class Dead:
            def connect(self):
                raise RuntimeError("down")

        with patch("app.database.engine", Dead()):
            ok, detail = health._check_database()
        self.assertFalse(ok)
        self.assertIn("unreachable", detail)


class MetricsTests(unittest.TestCase):
    def setUp(self):
        self.reg = metrics.Registry()

    def test_counter_and_gauge_render_in_prometheus_format(self):
        self.reg.describe("things_total", "Things")
        self.reg.inc("things_total", {"kind": "a"})
        self.reg.inc("things_total", {"kind": "a"}, 2)
        self.reg.inc("things_total", {"kind": "b"})
        self.reg.set_gauge("level", 7)
        text = self.reg.render()
        self.assertIn("# TYPE things_total counter", text)
        self.assertIn('things_total{kind="a"} 3', text)
        self.assertIn('things_total{kind="b"} 1', text)
        self.assertIn("# TYPE level gauge", text)
        self.assertIn("level 7", text)

    def test_histogram_buckets_are_cumulative(self):
        for seconds in (0.04, 0.3, 3.0):
            self.reg.observe("req_seconds", seconds, buckets=(0.1, 1, 5))
        text = self.reg.render()
        self.assertIn('req_seconds_bucket{le="0.1"} 1', text)
        self.assertIn('req_seconds_bucket{le="1"} 2', text)
        self.assertIn('req_seconds_bucket{le="5"} 3', text)
        self.assertIn('req_seconds_bucket{le="+Inf"} 3', text)
        self.assertIn("req_seconds_count 3", text)

    def test_label_values_are_escaped(self):
        self.reg.inc("x_total", {"v": 'a"b\\c\nd'})
        self.assertIn(r'x_total{v="a\"b\\c\nd"} 1', self.reg.render())

    def test_http_provider_helpers(self):
        metrics.registry.reset()
        metrics.record_http("GET", 404, 0.02)
        metrics.record_http("POST", 200, 0.5)
        metrics.record_provider("gemini", "success")
        self.assertEqual(metrics.registry.counter_value("moraa_http_requests_total", {"method": "GET", "status": "4xx"}), 1)
        self.assertEqual(metrics.registry.counter_value("moraa_provider_calls_total", {"provider": "gemini", "outcome": "success"}), 1)

    def test_database_gauges_are_filled_from_real_data(self):
        engine = make_engine(concurrent=True)
        Base.metadata.create_all(bind=engine)
        Session = sessionmaker(bind=engine)
        self.addCleanup(engine.dispose)
        with Session() as db:
            db.add_all([
                WhatsAppIngestion(external_user_id="919800000001", external_message_id="m1", external_media_id="x",
                                  channel="whatsapp", status="delivered"),
                WhatsAppIngestion(external_user_id="919800000001", external_message_id="m2", external_media_id="x",
                                  channel="whatsapp", status="failed"),
                PendingPayment(payment_id="pay_1", amount_rupees=500, currency="INR", reason="missing_phone"),
            ])
            db.commit()
        metrics.registry.reset()
        spend_counter.reset_for_tests()
        with patch("app.database.SessionLocal", Session):
            metrics.collect_database_gauges(force=True)
        text = metrics.registry.render()
        self.assertIn('moraa_orders{status="delivered"} 1', text)
        self.assertIn('moraa_orders{status="failed"} 1', text)
        self.assertIn("moraa_pending_payments 1", text)
        self.assertIn("moraa_db_up 1", text)

    def test_a_database_problem_marks_db_down_and_never_raises(self):
        metrics.registry.reset()
        with patch("app.database.SessionLocal", side_effect=RuntimeError("down")):
            metrics.collect_database_gauges(force=True)
        self.assertIn("moraa_db_up 0", metrics.registry.render())


class ErrorScrubTests(unittest.TestCase):
    def test_bodies_cookies_auth_headers_user_and_variables_are_removed(self):
        event = {
            "request": {"data": {"name": "Asha", "gstin": "27AAAAA0000A1Z5"}, "cookies": {"s": "x"},
                        "query_string": "phone=919812345678",
                        "headers": {"Authorization": "Bearer abc", "X-Hub-Signature-256": "sig", "Accept": "json"}},
            "user": {"id": "1", "ip_address": "1.2.3.4"},
            "message": "payment for 919812345678 failed",
            "exception": {"values": [{"value": "bad number 919812345678",
                                      "stacktrace": {"frames": [{"function": "f", "vars": {"token": "secret"}}]}}]},
        }
        out = scrub_event(event)
        self.assertNotIn("data", out["request"])
        self.assertNotIn("cookies", out["request"])
        self.assertNotIn("query_string", out["request"])
        self.assertEqual(out["request"]["headers"]["Authorization"], "[removed]")
        self.assertEqual(out["request"]["headers"]["X-Hub-Signature-256"], "[removed]")
        self.assertEqual(out["request"]["headers"]["Accept"], "json")
        self.assertNotIn("user", out)
        self.assertNotIn("919812345678", out["message"])
        self.assertNotIn("919812345678", out["exception"]["values"][0]["value"])
        self.assertNotIn("vars", out["exception"]["values"][0]["stacktrace"]["frames"][0])

    def test_tracking_stays_off_without_a_dsn(self):
        from app.services.error_tracking import init_error_tracking

        with patch.object(settings, "SENTRY_DSN", ""):
            self.assertFalse(init_error_tracking())

    def test_a_dsn_without_the_package_does_not_stop_startup(self):
        from app.services.error_tracking import init_error_tracking

        with patch.object(settings, "SENTRY_DSN", "https://key@example.ingest.sentry.io/1"), \
             patch.dict("sys.modules", {"sentry_sdk": None}):
            self.assertFalse(init_error_tracking())


class AlertTests(unittest.TestCase):
    def setUp(self):
        self.engine = make_engine(concurrent=True)
        Base.metadata.create_all(bind=self.engine)
        self.Session = sessionmaker(bind=self.engine)
        self.db = self.Session()
        self.addCleanup(self.engine.dispose)
        self.addCleanup(self.db.close)
        spend_counter.reset_for_tests()
        patcher = patch("app.database.SessionLocal", self.Session)
        patcher.start()
        self.addCleanup(patcher.stop)

    def order(self, mid, status, charged=0, age_minutes=0):
        row = WhatsAppIngestion(external_user_id="919800000001", external_message_id=mid, external_media_id="x",
                                channel="whatsapp", status=status, amount_charged=charged)
        self.db.add(row)
        self.db.commit()
        if age_minutes:
            self.db.query(WhatsAppIngestion).filter(WhatsAppIngestion.id == row.id).update(
                {"updated_at": datetime.now(timezone.utc) - timedelta(minutes=age_minutes)})
            self.db.commit()

    def keys(self):
        return {a.key for a in alert_service.evaluate_alerts(self.db)}

    def test_a_quiet_system_raises_nothing(self):
        self.assertEqual(self.keys(), set())

    def test_failure_rate_needs_enough_orders_to_mean_something(self):
        self.order("m1", "failed")
        self.order("m2", "failed")
        self.assertNotIn("failure_rate", self.keys())                  # 2 of 2 failed, but only 2 orders
        for i in range(3, 8):
            self.order(f"m{i}", "failed")
        self.assertIn("failure_rate", self.keys())

    def test_parked_payments_reviews_and_stuck_orders(self):
        self.db.add(PendingPayment(payment_id="pay_1", amount_rupees=500, currency="INR", reason="missing_phone"))
        self.db.add(AuditLog(user_id=None, action="razorpay_clawback", resource_id="rfnd_1",
                             resource_type="razorpay_payment", status="pending"))
        self.db.commit()
        self.order("s1", "processing", charged=250, age_minutes=45)
        self.order("s2", "processing", charged=250, age_minutes=2)         # recent: not stuck
        keys = self.keys()
        self.assertTrue({"parked_payments", "payment_reviews", "stuck_orders"} <= keys)
        stuck = [a for a in alert_service.evaluate_alerts(self.db) if a.key == "stuck_orders"][0]
        self.assertIn("1 paid order", stuck.message)

    def test_spend_near_the_cap(self):
        from app.ai import image_generation_manager as igm

        with patch.object(settings, "MAX_GENERATIONS_PER_DAY", 100):
            spend_counter.reserve(igm.current_spend_day(), 79, None)
            self.assertNotIn("spend_near_cap", self.keys())
            spend_counter.reserve(igm.current_spend_day(), 1, None)
            self.assertIn("spend_near_cap", self.keys())

    def test_one_broken_check_does_not_hide_the_others(self):
        self.db.add(PendingPayment(payment_id="pay_1", amount_rupees=500, currency="INR", reason="missing_phone"))
        self.db.commit()
        with patch("app.services.generation_metrics.compute_generation_failure_rate", side_effect=RuntimeError("boom")):
            self.assertIn("parked_payments", self.keys())

    def test_alerts_are_sent_once_per_cooldown_and_contain_no_customer_data(self):
        alerts = [alert_service.Alert("parked_payments", "Moraa alert: 1 paid Razorpay payment(s), ₹500 in total.")]
        send = AsyncMock(return_value=True)
        with patch.object(settings, "OPS_ALERT_WHATSAPP_NUMBERS", "919811111111, 919822222222"), \
             patch("app.services.meta_whatsapp_service.send_whatsapp_text", new=send):
            first = asyncio.run(alert_service.dispatch_alerts(self.db, alerts))
            second = asyncio.run(alert_service.dispatch_alerts(self.db, alerts))     # still in its cooldown
        self.assertEqual((first, second), (["parked_payments"], []))
        self.assertEqual(send.await_count, 2)                                        # two numbers, once
        self.assertEqual(self.db.query(AuditLog).filter(AuditLog.action == "ops_alert_sent").count(), 1)

    def test_the_alert_repeats_after_the_cooldown(self):
        alerts = [alert_service.Alert("stuck_orders", "x")]
        with patch.object(settings, "OPS_ALERT_WHATSAPP_NUMBERS", "919811111111"), \
             patch("app.services.meta_whatsapp_service.send_whatsapp_text", new=AsyncMock(return_value=True)):
            asyncio.run(alert_service.dispatch_alerts(self.db, alerts))
            self.db.query(AuditLog).update({"created_at": datetime.now(timezone.utc) - timedelta(hours=3)})
            self.db.commit()
            again = asyncio.run(alert_service.dispatch_alerts(self.db, alerts))
        self.assertEqual(again, ["stuck_orders"])

    def test_if_nothing_is_delivered_the_cooldown_does_not_start(self):
        alerts = [alert_service.Alert("stuck_orders", "x")]
        with patch.object(settings, "OPS_ALERT_WHATSAPP_NUMBERS", "919811111111"), \
             patch("app.services.meta_whatsapp_service.send_whatsapp_text", new=AsyncMock(return_value=False)):
            self.assertEqual(asyncio.run(alert_service.dispatch_alerts(self.db, alerts)), [])
        self.assertEqual(self.db.query(AuditLog).filter(AuditLog.action == "ops_alert_sent").count(), 0)

    def test_with_no_numbers_configured_alerts_are_only_logged(self):
        alerts = [alert_service.Alert("stuck_orders", "x")]
        with patch.object(settings, "OPS_ALERT_WHATSAPP_NUMBERS", ""):
            self.assertEqual(asyncio.run(alert_service.dispatch_alerts(self.db, alerts)), ["stuck_orders"])

    def test_failure_rate_check_in_the_order_path_is_throttled(self):
        from app.services import meta_whatsapp_service as mws

        calls = []
        with patch("app.services.generation_metrics.alert_if_failure_rate_exceeded", side_effect=lambda db: calls.append(1)):
            mws._last_failure_rate_check = 0.0
            mws._check_failure_rate(self.db)
            mws._check_failure_rate(self.db)
            mws._check_failure_rate(self.db)
        self.assertEqual(len(calls), 1)                                            # one query, not one per failed order


if __name__ == "__main__":
    unittest.main()
