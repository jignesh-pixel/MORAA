"""The test-database factory: right engine per mode, and it can never reach a real deployment."""

import os
import unittest
from unittest.mock import patch

from tests import db_support
from tests.db_support import assert_safe_test_url, make_engine, postgres_mode


class EngineFollowsModeTests(unittest.TestCase):
    def test_make_engine_matches_the_selected_backend(self):
        engine = make_engine()
        try:
            expected = "postgresql" if postgres_mode() else "sqlite"
            self.assertEqual(engine.dialect.name, expected)
        finally:
            engine.dispose()

    def test_postgres_engines_are_isolated_schemas_that_vanish_on_dispose(self):
        if not postgres_mode():
            self.skipTest("PostgreSQL mode only")
        from sqlalchemy import text

        first, second = make_engine(), make_engine()
        try:
            self.assertNotEqual(first.moraa_schema, second.moraa_schema)
            with first.begin() as conn:
                conn.execute(text("CREATE TABLE probe (id int)"))
            with second.connect() as conn:
                self.assertEqual(conn.execute(text("SELECT to_regclass('probe')")).scalar(), None)
        finally:
            schema = first.moraa_schema
            first.dispose()
            second.dispose()
        with db_support._admin().connect() as conn:
            self.assertEqual(
                conn.execute(text("SELECT count(*) FROM pg_namespace WHERE nspname = :s"), {"s": schema}).scalar(), 0
            )


class SafetyGuardTests(unittest.TestCase):
    def test_hosted_production_style_urls_are_refused(self):
        for url in (
            "postgresql://postgres.abcd:pw@aws-0-ap-south-1.pooler.supabase.com:6543/postgres",
            "postgresql://u:p@db.abcd.supabase.co:5432/postgres",
        ):
            with self.assertRaises(RuntimeError, msg=url):
                assert_safe_test_url(url)

    def test_non_local_hosts_need_an_explicit_override(self):
        url = "postgresql://u:p@db.example.internal:5432/app_test"
        with patch.dict(os.environ, {}, clear=False) as env:
            env.pop("MORAA_ALLOW_REMOTE_TEST_DB", None)
            with self.assertRaises(RuntimeError):
                assert_safe_test_url(url)
        with patch.dict(os.environ, {"MORAA_ALLOW_REMOTE_TEST_DB": "1"}):
            assert_safe_test_url(url)

    def test_override_never_unlocks_a_supabase_host(self):
        with patch.dict(os.environ, {"MORAA_ALLOW_REMOTE_TEST_DB": "1"}):
            with self.assertRaises(RuntimeError):
                assert_safe_test_url("postgresql://u:p@db.abcd.supabase.co:5432/postgres")

    def test_query_string_cannot_redirect_the_connection(self):
        for url in (
            "postgresql://u:p@localhost/x?host=db.prod.example",
            "postgresql://u:p@localhost/x?hostaddr=10.1.1.1",
            "postgresql://u:p@localhost/x?service=prod",
            "postgresql://u:p@localhost/x?host=db.abcd.supabase.co",
        ):
            with self.assertRaises(RuntimeError, msg=url):
                assert_safe_test_url(url)

    def test_every_host_in_a_multi_host_url_is_checked(self):
        with self.assertRaises(RuntimeError):
            assert_safe_test_url("postgresql://u:p@localhost,db.example.internal/x")
        with self.assertRaises(RuntimeError):
            assert_safe_test_url("postgresql://u:p@/x")          # no host at all (unix socket / env default)

    def test_loopback_hosts_are_allowed(self):
        for host in ("localhost", "127.0.0.1", "[::1]"):
            assert_safe_test_url(f"postgresql://u:p@{host}:5432/moraa_test")


if __name__ == "__main__":
    unittest.main()
