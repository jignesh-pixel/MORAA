"""Phase 4 / DATA-2, DATA-3, DATA-4: autogenerate sees the real models, hot-path indexes exist after migrating,
and the analyses list is paged with its images loaded in one extra query."""

import unittest
from datetime import datetime, timedelta, timezone

from sqlalchemy import event, inspect
from sqlalchemy.orm import sessionmaker

import app.models  # noqa: F401 -- register every model with Base.metadata
from app.database import Base, get_schema_status, upgrade_to_head
from app.models.analysis import Analysis
from app.models.image import Image
from app.services.analysis_service import AnalysisService
from tests.db_support import make_engine

EXPECTED_INDEXES = {
    "whatsapp_ingestions": {"ix_whatsapp_ingestions_status_updated", "ix_whatsapp_ingestions_created_at",
                            "ix_whatsapp_ingestions_user_status"},
    "audit_logs": {"ix_audit_logs_action_resource", "ix_audit_logs_action_created"},
}


class IndexTests(unittest.TestCase):
    def test_migrations_create_the_hot_path_indexes_and_match_the_models(self):
        engine = make_engine()
        self.addCleanup(engine.dispose)
        upgrade_to_head(engine)
        self.assertTrue(get_schema_status(engine).up_to_date)
        insp = inspect(engine)
        for table, names in EXPECTED_INDEXES.items():
            have = {ix["name"] for ix in insp.get_indexes(table)}
            self.assertTrue(names <= have, f"{table} missing {sorted(names - have)}")
        # create_all (tests, one-off tools) defines the same indexes from the models
        fresh = make_engine()
        self.addCleanup(fresh.dispose)
        Base.metadata.create_all(bind=fresh)
        for table, names in EXPECTED_INDEXES.items():
            self.assertTrue(names <= {ix["name"] for ix in inspect(fresh).get_indexes(table)})

    def test_upgrading_twice_keeps_the_indexes_and_does_not_fail(self):
        engine = make_engine()
        self.addCleanup(engine.dispose)
        upgrade_to_head(engine)
        upgrade_to_head(engine)
        self.assertIn("ix_audit_logs_action_resource", {ix["name"] for ix in inspect(engine).get_indexes("audit_logs")})

    def test_alembic_sees_every_model(self):
        """DATA-2: env.py imports app.models, so autogenerate compares against the real metadata."""
        from pathlib import Path

        env = (Path(__file__).resolve().parent.parent / "alembic" / "env.py").read_text(encoding="utf-8")
        self.assertIn("import app.models", env)
        self.assertIn("target_metadata = Base.metadata", env)
        self.assertTrue({"customers", "wallet_transactions", "whatsapp_ingestions", "audit_logs", "generation_spend",
                         "pending_payments"} <= set(Base.metadata.tables))


class AnalysisPagingTests(unittest.TestCase):
    def setUp(self):
        self.engine = make_engine()
        Base.metadata.create_all(bind=self.engine)
        self.db = sessionmaker(bind=self.engine)()
        self.addCleanup(self.engine.dispose)
        self.addCleanup(self.db.close)
        now = datetime.now(timezone.utc)
        for i in range(25):
            image = Image(original_filename=f"i{i}.jpg", stored_filename=f"i{i}.jpg", file_path=f"/x/{i}.jpg",
                          file_size=10, mime_type="image/jpeg", image_url=f"/uploads/{i}.jpg")
            self.db.add(image)
            self.db.flush()
            self.db.add(Analysis(image_id=image.id, request_id=image.request_id, status="completed",
                                 analyzed_at=now + timedelta(minutes=i)))
        other = Image(original_filename="p.jpg", stored_filename="p.jpg", file_path="/x/p.jpg", file_size=10,
                      mime_type="image/jpeg", image_url="/uploads/p.jpg")
        self.db.add(other)
        self.db.flush()
        self.db.add(Analysis(image_id=other.id, request_id=other.request_id, status="processing"))
        self.db.commit()

    def test_pages_are_newest_first_and_stable(self):
        service = AnalysisService(self.db)
        first = service.get_analysis_history(limit=10, offset=0)
        second = service.get_analysis_history(limit=10, offset=10)
        third = service.get_analysis_history(limit=10, offset=20)
        self.assertEqual((len(first), len(second), len(third)), (10, 10, 5))
        ids = [a.id for a in first + second + third]
        self.assertEqual(len(set(ids)), 25)                                  # no overlap between pages
        times = [a.analyzedAt for a in first + second + third]
        self.assertEqual(times, sorted(times, reverse=True))                 # newest first
        self.assertTrue(all(a.status == "completed" for a in first))        # only completed analyses

    def test_the_page_size_is_capped(self):
        service = AnalysisService(self.db)
        self.assertEqual(len(service.get_analysis_history(limit=100000)), 25)
        self.assertEqual(len(service.get_analysis_history(limit=0)), 1)      # at least one row, never an unbounded query

    def test_images_are_loaded_in_one_extra_query_not_one_per_row(self):
        statements = []

        @event.listens_for(self.engine, "before_cursor_execute")
        def count(conn, cursor, statement, *a):
            statements.append(statement)

        self.db.expire_all()
        AnalysisService(self.db).get_analysis_history(limit=25)
        selects = [s for s in statements if s.lstrip().upper().startswith("SELECT")]
        self.assertLessEqual(len(selects), 2, selects)                       # analyses + images, not 1 + 25


if __name__ == "__main__":
    unittest.main()
