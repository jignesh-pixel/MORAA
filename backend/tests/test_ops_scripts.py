"""Phase 4 / DEP-4, DEP-5: backup, restore-check and release scripts (their logic; pg_dump itself is not run)."""

import importlib.util
import sys
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

from sqlalchemy import text

from app.database import SchemaStatus, upgrade_to_head
from tests.db_support import make_engine

SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"


def _load(name):
    spec = importlib.util.spec_from_file_location(f"scripts_{name}", SCRIPTS / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


db_backup = _load("db_backup")
db_restore_check = _load("db_restore_check")
release = _load("release")

URL = "postgresql://postgres.abc123:S3cr3t%21pw@aws-0-ap.pooler.supabase.com:6543/postgres?sslmode=require"


class BackupTests(unittest.TestCase):
    def test_the_password_goes_in_the_environment_never_on_the_command_line(self):
        argv, env = db_backup.build_dump_command(URL, Path("out.dump"))
        self.assertEqual(env["PGPASSWORD"], "S3cr3t!pw")
        self.assertEqual(env["PGSSLMODE"], "require")
        self.assertNotIn("S3cr3t", " ".join(argv))
        self.assertNotIn("S3cr3t%21pw", " ".join(argv))
        self.assertIn("--format=custom", argv)
        self.assertIn("--host=aws-0-ap.pooler.supabase.com", argv)
        self.assertIn("--port=6543", argv)
        self.assertEqual(argv[0], "pg_dump")

    def test_only_postgresql_is_backed_up(self):
        with self.assertRaises(ValueError):
            db_backup.build_dump_command("sqlite:///x.db", Path("out.dump"))

    def test_backup_names_sort_oldest_first(self):
        a = db_backup.backup_name(datetime(2026, 10, 1, 2, 0, 0))
        b = db_backup.backup_name(datetime(2026, 10, 2, 2, 0, 0))
        self.assertEqual(a, "moraa-20261001-020000.dump")
        self.assertLess(a, b)

    def test_pruning_keeps_the_newest_and_leaves_other_files_alone(self):
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp)
            for day in range(1, 6):
                (folder / f"moraa-2026100{day}-020000.dump").write_bytes(b"x")
            (folder / "notes.txt").write_text("keep me")
            removed = db_backup.prune_old_backups(folder, keep=2)
            self.assertEqual(sorted(p.name for p in removed),
                             ["moraa-20261001-020000.dump", "moraa-20261002-020000.dump", "moraa-20261003-020000.dump"])
            self.assertEqual(sorted(p.name for p in folder.iterdir()),
                             ["moraa-20261004-020000.dump", "moraa-20261005-020000.dump", "notes.txt"])

    def test_pruning_never_deletes_everything(self):
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp)
            (folder / "moraa-20261001-020000.dump").write_bytes(b"x")
            db_backup.prune_old_backups(folder, keep=0)
            self.assertEqual(len(list(folder.glob("*.dump"))), 1)

    def test_a_missing_pg_dump_is_a_clear_failure(self):
        with patch.object(db_backup.shutil, "which", return_value=None):
            self.assertEqual(db_backup.main(["--out-dir", tempfile.gettempdir()]), 2)


class RestoreCheckTests(unittest.TestCase):
    def test_supabase_targets_are_refused(self):
        for host in ("db.abc.supabase.co", "aws-0.pooler.supabase.com"):
            with self.assertRaises(ValueError):
                db_restore_check.check_target(f"postgresql://u:p@{host}:5432/postgres")
        db_restore_check.check_target("postgresql://u:p@localhost:5432/scratch")          # local scratch is fine
        with self.assertRaises(ValueError):
            db_restore_check.check_target("sqlite:///x.db")

    def test_the_restore_command_keeps_the_password_out_of_the_arguments(self):
        argv, env = db_restore_check.build_restore_command("postgresql://u:pw123@localhost:5432/scratch", Path("a.dump"))
        self.assertNotIn("pw123", " ".join(argv))
        self.assertEqual(env, {"PGPASSWORD": "pw123"})
        self.assertIn("--clean", argv)

    def test_a_restore_without_the_env_var_fails_clearly(self):
        with patch.dict("os.environ", {}, clear=False):
            import os

            os.environ.pop("RESTORE_TEST_DATABASE_URL", None)
            self.assertEqual(db_restore_check.main(["whatever.dump"]), 2)

    def _engine_with(self, rows):
        engine = make_engine()
        self.addCleanup(engine.dispose)
        upgrade_to_head(engine)
        with engine.begin() as conn:
            for cid, balance, ledger in rows:
                conn.execute(text("INSERT INTO customers (id, whatsapp_id, full_name, business_name, gst_number, address, "
                                  "wallet_balance) VALUES (:i, :w, 'T', 'B', 'N/A', 'A', :b)"),
                             {"i": cid, "w": f"91900000{cid}", "b": balance})
                for n, amount in enumerate(ledger):
                    conn.execute(text("INSERT INTO wallet_transactions (id, customer_id, kind, amount, balance_after, created_at) "
                                      "VALUES (:id, :c, 'credit_payment', :a, :a, '2026-10-02 00:00:00')"),
                                 {"id": f"{cid}-{n}", "c": cid, "a": amount})
        return engine

    def test_matching_ledgers_pass(self):
        engine = self._engine_with([("c1", 500, [300, 200]), ("c2", 0, [])])
        self.assertEqual(db_restore_check.verify_restored_money(engine), [])

    def test_a_balance_that_differs_from_its_ledger_is_reported(self):
        engine = self._engine_with([("c1", 999, [300, 200])])
        problems = db_restore_check.verify_restored_money(engine)
        self.assertEqual(len(problems), 1)
        self.assertIn("differs from the sum of their ledger", problems[0])

    def test_an_empty_restore_is_reported(self):
        engine = self._engine_with([])
        self.assertTrue(any("no customers" in p for p in db_restore_check.verify_restored_money(engine)))


class ReleaseTests(unittest.TestCase):
    def run_release(self, status, index=True, after=None, upgrade_error=None, args=()):
        calls = {"upgrade": 0}
        statuses = [status] + ([after] if after else [])

        def fake_status():
            return statuses.pop(0) if len(statuses) > 1 else statuses[0]

        def fake_upgrade(engine):
            calls["upgrade"] += 1
            if upgrade_error:
                raise upgrade_error

        with patch("app.database.get_schema_status", side_effect=fake_status), \
             patch("app.database.upgrade_to_head", side_effect=fake_upgrade), \
             patch("app.database.money_once_index_present", return_value=index):
            code = release.main(list(args))
        return code, calls["upgrade"]

    behind = SchemaStatus(current="0008_customer_access_tiers", head="0015_auth_hardening", has_app_tables=True)
    current = SchemaStatus(current="0015_auth_hardening", head="0015_auth_hardening", has_app_tables=True)

    def test_a_database_behind_the_code_is_upgraded_then_verified(self):
        self.assertEqual(self.run_release(self.behind, after=self.current), (0, 1))

    def test_an_up_to_date_database_is_not_touched(self):
        self.assertEqual(self.run_release(self.current), (0, 0))

    def test_check_only_never_upgrades_and_fails_when_behind(self):
        self.assertEqual(self.run_release(self.behind, args=["--check-only"]), (1, 0))

    def test_a_failed_upgrade_means_do_not_start(self):
        self.assertEqual(self.run_release(self.behind, upgrade_error=RuntimeError("boom")), (1, 1))

    def test_a_missing_money_once_index_blocks_the_release(self):
        self.assertEqual(self.run_release(self.current, index=False)[0], 1)


class RestoreTargetGuardTests(unittest.TestCase):
    def test_the_applications_own_database_and_non_scratch_names_are_refused(self):
        live = "postgresql://u:p@db.internal:5432/restore_live"
        with self.assertRaises(ValueError):
            db_restore_check.check_target("postgresql://u:p@db.internal:5432/restore_live", live)
        with self.assertRaises(ValueError):
            db_restore_check.check_target("postgresql://u:p@localhost:5432/moraa", live)       # name must say scratch
        db_restore_check.check_target("postgresql://u:p@localhost:5432/restore_scratch", live)


class MetricLabelTests(unittest.TestCase):
    def test_unknown_http_methods_share_one_label(self):
        from app.services import metrics
        metrics.registry.reset()
        metrics.record_http("WEIRD-1", 200, 0.1)
        metrics.record_http("WEIRD-2", 200, 0.1)
        self.assertIn('method="OTHER"', metrics.registry.render())
        self.assertNotIn("WEIRD", metrics.registry.render())


if __name__ == "__main__":
    unittest.main()
