"""P2·04 — windowed generation failure-rate monitoring."""

import os
import sys
import unittest
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import app.models  # noqa: F401 — register every model with Base.metadata
from app.database import Base
from app.models.whatsapp_ingestion import WhatsAppIngestion
from app.services.generation_metrics import compute_generation_failure_rate


def _make_engine_and_session():
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(bind=engine)
    return engine, sessionmaker(bind=engine)()


def _make_ingestion(db, status, message_id, updated_at=None):
    ingestion = WhatsAppIngestion(
        external_user_id="919876543210",
        external_message_id=message_id,
        external_media_id="media",
        channel="whatsapp",
        mime_type="image/jpeg",
        status=status,
    )
    db.add(ingestion)
    db.commit()
    db.refresh(ingestion)
    if updated_at is not None:
        db.query(WhatsAppIngestion).filter(WhatsAppIngestion.id == ingestion.id).update(
            {WhatsAppIngestion.updated_at: updated_at}
        )
        db.commit()
    return ingestion


class GenerationFailureRateTests(unittest.TestCase):
    def setUp(self):
        self.engine, self.db = _make_engine_and_session()

    def tearDown(self):
        self.db.close()
        self.engine.dispose()

    def test_no_terminal_attempts_reports_none_not_zero(self):
        _make_ingestion(self.db, "pending_payment", "wamid.1")
        report = compute_generation_failure_rate(self.db)

        self.assertEqual(report.total_attempts, 0)
        self.assertIsNone(report.failure_rate)
        self.assertFalse(report.exceeds_target)

    def test_mixed_terminal_statuses_computed_correctly(self):
        _make_ingestion(self.db, "delivered", "wamid.1")
        _make_ingestion(self.db, "generated", "wamid.2")
        _make_ingestion(self.db, "failed", "wamid.3")
        _make_ingestion(self.db, "delivery_failed", "wamid.4")
        # Non-terminal — excluded from both sides.
        _make_ingestion(self.db, "processing", "wamid.5")
        _make_ingestion(self.db, "pending_payment", "wamid.6")

        report = compute_generation_failure_rate(self.db)

        self.assertEqual(report.total_attempts, 4)
        self.assertEqual(report.failed_attempts, 2)
        self.assertEqual(report.failure_rate, 0.5)
        self.assertTrue(report.exceeds_target)

    def test_attempts_outside_the_window_are_excluded(self):
        stale = datetime.now(timezone.utc) - timedelta(hours=48)
        _make_ingestion(self.db, "failed", "wamid.old", updated_at=stale)
        _make_ingestion(self.db, "delivered", "wamid.new")

        report = compute_generation_failure_rate(self.db, window_hours=24)

        self.assertEqual(report.total_attempts, 1)
        self.assertEqual(report.failed_attempts, 0)
        self.assertEqual(report.failure_rate, 0.0)
        self.assertFalse(report.exceeds_target)

    def test_below_target_does_not_exceed(self):
        for i in range(9):
            _make_ingestion(self.db, "delivered", f"wamid.ok.{i}")
        _make_ingestion(self.db, "failed", "wamid.bad")

        report = compute_generation_failure_rate(self.db)

        self.assertEqual(report.total_attempts, 10)
        self.assertAlmostEqual(report.failure_rate, 0.1)
        self.assertFalse(report.exceeds_target)


if __name__ == "__main__":
    unittest.main()
