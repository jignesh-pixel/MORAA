"""Alembic is the only owner of the schema (Phase 1, DATA-01).

Runs on SQLite by default and on PostgreSQL with ``MORAA_TEST_DB=postgres``.
"""

import os
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from alembic import command
from alembic.autogenerate import compare_metadata
from alembic.runtime.migration import MigrationContext
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.orm import sessionmaker

import app.models  # noqa: F401 -- register every model with Base.metadata
from app.config import BASE_DIR, settings
from app.database import (
    Base,
    alembic_config,
    ensure_schema_ready,
    get_schema_status,
    upgrade_to_head,
)
from app.models.customer import Customer
from tests.db_support import make_engine, postgres, requires_postgres, sqlite_engine

APP_TABLES = set(Base.metadata.tables)

# Differences between "migrations from empty" and the models that exist TODAY.
# Phase 1 does not change the schema, so they are pinned instead of fixed: a NEW
# difference fails this test, and fixing one means removing it from the set (the
# financial-core phase owns those schema changes).
#   * created_at / updated_at are nullable in the migrations but NOT NULL in the models
#   * analyses.raw_response is TEXT in the database and a JSON TypeDecorator in the model
KNOWN_DRIFT = {
    ("modify_nullable", "customers", "created_at"),
    ("modify_nullable", "customers", "updated_at"),
    ("modify_nullable", "onboarding_sessions", "created_at"),
    ("modify_nullable", "onboarding_sessions", "updated_at"),
    ("modify_nullable", "whatsapp_payment_orders", "created_at"),
    ("modify_nullable", "whatsapp_payment_orders", "updated_at"),
    ("modify_type", "analyses", "raw_response"),
}


BASELINE = "ebdee18538d1"   # core schema baseline; its downgrade is a deliberate no-op (data safety)


def _downgrade(engine, revision):
    cfg = alembic_config()
    with engine.begin() as connection:
        cfg.attributes["connection"] = connection
        command.downgrade(cfg, revision)


class MigrationChainTests(unittest.TestCase):
    def setUp(self):
        self.engine = make_engine()

    def tearDown(self):
        self.engine.dispose()

    def test_empty_database_upgrades_to_head_with_every_model_table(self):
        before = get_schema_status(self.engine)
        self.assertIsNone(before.current)
        self.assertFalse(before.has_app_tables)

        upgrade_to_head(self.engine)

        after = get_schema_status(self.engine)
        self.assertTrue(after.up_to_date, after)
        tables = set(inspect(self.engine).get_table_names())
        self.assertTrue(APP_TABLES <= tables, f"missing tables: {sorted(APP_TABLES - tables)}")

    def test_upgrading_twice_is_a_no_op(self):
        upgrade_to_head(self.engine)
        first = get_schema_status(self.engine)
        upgrade_to_head(self.engine)
        self.assertEqual(get_schema_status(self.engine), first)

    def test_money_once_index_exists_after_migrations(self):
        upgrade_to_head(self.engine)
        names = {ix["name"] for ix in inspect(self.engine).get_indexes("audit_logs")}
        self.assertIn("uq_audit_logs_money_once", names)

    def test_migrations_after_the_baseline_downgrade_and_upgrade_again(self):
        """0003-0008 can be rolled back to the baseline and re-applied."""
        upgrade_to_head(self.engine)
        _downgrade(self.engine, BASELINE)
        insp = inspect(self.engine)
        self.assertEqual(get_schema_status(self.engine).current, BASELINE)
        self.assertNotIn("whatsapp_payment_orders", insp.get_table_names())             # 0005 undone
        self.assertNotIn("tier", {c["name"] for c in insp.get_columns("customers")})    # 0008 undone
        self.assertNotIn("uq_audit_logs_money_once", {ix["name"] for ix in insp.get_indexes("audit_logs")})  # 0003 undone
        upgrade_to_head(self.engine)
        self.assertTrue(get_schema_status(self.engine).up_to_date)

    def test_downgrading_below_the_baseline_keeps_the_baseline_tables(self):
        """The baseline's downgrade is intentionally a no-op, so its tables survive a deep rollback.

        KNOWN HAZARD (documented, not changed in Phase 1): migrations 0001/0002 sit BELOW the
        baseline and their downgrades drop `onboarding_sessions` and `customers` (wallet balances!).
        `alembic downgrade base` on a live database therefore destroys the wallet table. Never run it
        on production; the financial-core phase should make 0002's downgrade non-destructive.
        """
        upgrade_to_head(self.engine)
        _downgrade(self.engine, "base")
        tables = set(inspect(self.engine).get_table_names())
        baseline_tables = {"users", "images", "analyses", "audit_logs", "whatsapp_ingestions", "processing_logs"}
        self.assertTrue(baseline_tables <= tables, f"dropped: {sorted(baseline_tables - tables)}")
        self.assertNotIn("customers", tables)           # the hazard above, pinned so a fix is noticed
        self.assertNotIn("onboarding_sessions", tables)

    def test_ledger_migration_backfills_an_opening_balance_per_customer(self):
        """0009 must make SUM(ledger) == wallet_balance for customers that existed before the ledger."""
        cfg = alembic_config()
        with self.engine.begin() as connection:
            cfg.attributes["connection"] = connection
            command.upgrade(cfg, "0008_customer_access_tiers")
            connection.execute(text(
                "INSERT INTO customers (id, whatsapp_id, full_name, business_name, gst_number, address, wallet_balance) VALUES "
                "('c-rich', '919000000001', 'R', 'B', 'N/A', 'A', 750), ('c-zero', '919000000002', 'Z', 'B', 'N/A', 'A', 0)"
            ))
        upgrade_to_head(self.engine)
        with self.engine.connect() as conn:
            rows = conn.execute(text("SELECT customer_id, kind, amount, balance_after FROM wallet_transactions")).fetchall()
        self.assertEqual([tuple(r) for r in rows], [("c-rich", "opening_balance", 750, 750)])   # zero balance: no row
        upgrade_to_head(self.engine)                                                              # idempotent
        with self.engine.connect() as conn:
            self.assertEqual(conn.execute(text("SELECT count(*) FROM wallet_transactions")).scalar(), 1)

    def test_single_head(self):
        from alembic.script import ScriptDirectory

        self.assertEqual(len(ScriptDirectory.from_config(alembic_config()).get_heads()), 1)


@postgres
@requires_postgres
class ModelMigrationParityTests(unittest.TestCase):
    """Models and migrations must describe the same schema (comments aside)."""

    def setUp(self):
        self.engine = make_engine()
        upgrade_to_head(self.engine)

    def tearDown(self):
        self.engine.dispose()

    def test_no_new_drift_between_models_and_migrations(self):
        with self.engine.connect() as conn:
            context = MigrationContext.configure(conn, opts={"compare_type": True})
            raw = compare_metadata(context, Base.metadata)
        flat = []
        for entry in raw:
            flat.extend(entry if isinstance(entry, list) else [entry])
        drift = {
            (op[0], op[2], op[3]) if op[0].startswith("modify_") else (op[0], repr(op[1:]))
            for op in flat if op[0] != "modify_comment"
        }
        self.assertEqual(
            drift, KNOWN_DRIFT,
            f"new: {sorted(drift - KNOWN_DRIFT)}  fixed (remove from KNOWN_DRIFT): {sorted(KNOWN_DRIFT - drift)}",
        )


class EnsureSchemaReadyTests(unittest.TestCase):
    """The startup guard: Alembic head required in production, never create_all."""

    def setUp(self):
        # The dev-path tests must not depend on the runner's ENVIRONMENT.
        self.enterContext(patch.object(settings, "ENVIRONMENT", "development"))

    def test_database_newer_than_the_code_is_refused_in_production_with_a_clear_message(self):
        engine = sqlite_engine()
        try:
            upgrade_to_head(engine)
            with engine.begin() as conn:
                conn.execute(text("UPDATE alembic_version SET version_num = '0099_from_the_future'"))
            self.assertFalse(get_schema_status(engine).current_known)
            with patch.object(settings, "ENVIRONMENT", "production"):
                with self.assertRaises(RuntimeError) as ctx:
                    ensure_schema_ready(engine)
            self.assertIn("newer release", str(ctx.exception))
            self.assertNotIn("alembic upgrade head", str(ctx.exception))
        finally:
            engine.dispose()

    def test_database_newer_than_the_code_never_triggers_a_dev_migration(self):
        engine = sqlite_engine()
        try:
            upgrade_to_head(engine)
            with engine.begin() as conn:
                conn.execute(text("UPDATE alembic_version SET version_num = '0099_from_the_future'"))
            status = ensure_schema_ready(engine)          # logs an error, must not raise or migrate
            self.assertEqual(status.current, "0099_from_the_future")
        finally:
            engine.dispose()

    def test_startup_lifespan_checks_the_schema_and_never_creates_tables(self):
        from fastapi.testclient import TestClient

        import app.main as main_module
        from app.database import SchemaStatus

        self.assertFalse(hasattr(main_module, "init_db"))
        ready = SchemaStatus(current="x", head="x", has_app_tables=True)
        with patch.object(main_module, "ensure_schema_ready", return_value=ready) as check, \
             patch.object(Base.metadata, "create_all", side_effect=AssertionError("create_all at startup")):
            with TestClient(main_module.app):
                pass
        check.assert_called_once()

    def test_up_to_date_database_passes_in_production(self):
        engine = sqlite_engine()
        try:
            upgrade_to_head(engine)
            with patch.object(settings, "ENVIRONMENT", "production"):
                self.assertTrue(ensure_schema_ready(engine).up_to_date)
        finally:
            engine.dispose()

    def test_empty_database_is_refused_in_production_and_left_untouched(self):
        engine = sqlite_engine()
        try:
            with patch.object(settings, "ENVIRONMENT", "production"):
                with self.assertRaises(RuntimeError) as ctx:
                    ensure_schema_ready(engine)
            self.assertIn("alembic upgrade head", str(ctx.exception))
            self.assertEqual(inspect(engine).get_table_names(), [])        # nothing was created
        finally:
            engine.dispose()

    def test_create_all_database_without_version_is_refused_in_production(self):
        engine = sqlite_engine()
        try:
            Base.metadata.create_all(bind=engine)                            # what the old startup did
            with patch.object(settings, "ENVIRONMENT", "production"):
                with self.assertRaises(RuntimeError) as ctx:
                    ensure_schema_ready(engine)
            self.assertIn("never migrated", str(ctx.exception))
        finally:
            engine.dispose()

    def test_behind_head_database_is_refused_in_production(self):
        engine = sqlite_engine()
        try:
            upgrade_to_head(engine)
            with engine.begin() as conn:
                conn.execute(text("UPDATE alembic_version SET version_num = '0007_customer_is_gst_verified'"))
            with patch.object(settings, "ENVIRONMENT", "production"):
                with self.assertRaises(RuntimeError) as ctx:
                    ensure_schema_ready(engine)
            self.assertIn("0007_customer_is_gst_verified", str(ctx.exception))
        finally:
            engine.dispose()

    def test_development_sqlite_migrates_a_fresh_database_without_create_all(self):
        engine = sqlite_engine()
        try:
            with patch.object(Base.metadata, "create_all", side_effect=AssertionError("create_all must not run")):
                status = ensure_schema_ready(engine)
            self.assertTrue(status.up_to_date)
            self.assertTrue(APP_TABLES <= set(inspect(engine).get_table_names()))
        finally:
            engine.dispose()

    def test_development_sqlite_migrates_a_legacy_create_all_database_in_place(self):
        """A dev database made by the old create_all (no alembic_version) keeps its rows."""
        engine = sqlite_engine()
        try:
            Base.metadata.create_all(bind=engine)
            Session = sessionmaker(bind=engine)
            with Session() as db:
                db.add(Customer(whatsapp_id="919800000042", full_name="Legacy", business_name="B",
                                gst_number="N/A", address="A", wallet_balance=700, is_registered=True))
                db.commit()
            status = ensure_schema_ready(engine)
            self.assertTrue(status.up_to_date)
            with Session() as db:
                self.assertEqual(db.query(Customer).filter_by(whatsapp_id="919800000042").one().wallet_balance, 700)
        finally:
            engine.dispose()

    def test_two_migration_heads_are_refused(self):
        engine = sqlite_engine()
        try:
            with patch("alembic.script.ScriptDirectory.get_heads", return_value=["a1", "b2"]):
                with self.assertRaises(RuntimeError) as ctx:
                    get_schema_status(engine)
            self.assertIn("2 migration heads", str(ctx.exception))
        finally:
            engine.dispose()

    @postgres
    @requires_postgres
    def test_development_postgres_behind_head_is_reported_not_created(self):
        engine = make_engine()
        try:
            with patch.object(settings, "ENVIRONMENT", "development"):
                status = ensure_schema_ready(engine)
            self.assertFalse(status.up_to_date)
            self.assertEqual(APP_TABLES & set(inspect(engine).get_table_names()), set())
        finally:
            engine.dispose()


class CommandLineTests(unittest.TestCase):
    """The documented `alembic upgrade head` still works (env.py reads the URL from settings)."""

    def test_cli_upgrade_head_on_a_fresh_sqlite_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_file = os.path.join(tmp, "cli.db")
            env = dict(os.environ, MORAA_ENV_FILE="", DATABASE_URL=f"sqlite:///{db_file}")
            result = subprocess.run(
                [sys.executable, "-c", "from alembic.config import main; main(argv=['upgrade', 'head'])"],
                cwd=str(BASE_DIR), env=env, capture_output=True, text=True, timeout=180,
            )
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            engine = create_engine(f"sqlite:///{db_file}")
            try:
                self.assertTrue(get_schema_status(engine).up_to_date)
            finally:
                engine.dispose()

    def test_importing_alembic_gives_the_library_not_the_migrations_folder(self):
        import alembic

        self.assertTrue(hasattr(alembic, "context") or hasattr(alembic, "__version__"))
        self.assertNotEqual(os.path.dirname(os.path.abspath(alembic.__file__)), str(BASE_DIR / "alembic"))


if __name__ == "__main__":
    unittest.main()
