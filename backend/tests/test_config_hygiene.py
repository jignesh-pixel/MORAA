"""Phase 4 / CFG-3, CFG-4, CFG-5, CFG-6: separate developer switches, no duplicate or dead settings, current model
names, and loud warnings (not refusals) for production settings that are better set."""

import ast
import pathlib
import re
import unittest

from app.config import Settings

BACKEND = pathlib.Path(__file__).resolve().parent.parent
CONFIG_SOURCE = (BACKEND / "app" / "config.py").read_text(encoding="utf-8")


def _production(**overrides):
    base = dict(
        ENVIRONMENT="production", SECRET_KEY="k8Zq2vN7xP4mR9sT1wY6uB3eH5jL0dFa", META_APP_SECRET="x",
        RAZORPAY_WEBHOOK_SECRET="y", DATABASE_URL="postgresql://u:p@db.example.com:6543/postgres", DEBUG=False,
    )
    base.update(overrides)
    return Settings(_env_file=None, **base)


class SettingsHygieneTests(unittest.TestCase):
    def test_no_setting_is_declared_twice(self):
        tree = ast.parse(CONFIG_SOURCE)
        names = []
        for node in ast.walk(tree):
            if isinstance(node, ast.ClassDef) and node.name == "Settings":
                names = [i.target.id for i in node.body if isinstance(i, ast.AnnAssign) and isinstance(i.target, ast.Name)]
        duplicates = sorted({n for n in names if names.count(n) > 1})
        self.assertEqual(duplicates, [])

    def test_removed_dead_settings_stay_removed(self):
        for name in ("WORKERS", "ONBOARDING_PARSER_MODEL", "ONBOARDING_PARSER_MAX_CALLS_PER_DAY",
                     "ONBOARDING_PARSER_QUOTA_COOLDOWN_SECONDS", "ERPNEXT_TIMEOUT_SECONDS", "ENABLE_ONBOARDING_GATE"):
            self.assertNotIn(name, Settings.model_fields)

    def test_an_old_env_file_that_still_names_a_removed_setting_does_not_break_startup(self):
        s = Settings(_env_file=None, WORKERS="4", ONBOARDING_PARSER_MODEL="x")      # extra="ignore"
        self.assertFalse(hasattr(s, "WORKERS"))

    def test_every_setting_is_used_somewhere(self):
        """The guard against dead settings coming back: each field is read by app code, tests or config itself."""
        sources = "\n".join(p.read_text(encoding="utf-8", errors="ignore")
                            for p in list((BACKEND / "app").rglob("*.py")) if p.name != "config.py")
        sources += "\n" + re.sub(r"^\s+[A-Z_]+\s*:.*$", "", CONFIG_SOURCE, flags=re.M)   # uses inside config logic
        unused = [n for n in Settings.model_fields if not re.search(rf"\b{n}\b", sources)]
        self.assertEqual(unused, [])

    def test_model_defaults_are_not_the_retired_ones(self):
        s = Settings(_env_file=None)
        for retired in ("gemini-2.5-flash", "gemini-1.5-flash", "gemini-2.5-flash-image"):
            self.assertNotEqual(s.GEMINI_MODEL, retired)
            self.assertNotEqual(s.GEMINI_IMAGE_MODEL, retired)
        self.assertEqual(s.GEMINI_IMAGE_MODEL, "gemini-3.1-flash-image")

    def test_sql_echo_and_reload_are_their_own_switches_not_debug(self):
        s = Settings(_env_file=None, DEBUG="true")
        self.assertTrue(s.DEBUG)
        self.assertFalse(s.SQL_ECHO)
        self.assertFalse(s.DEV_RELOAD)
        from pathlib import Path

        database = Path(BACKEND / "app" / "database.py").read_text(encoding="utf-8")
        main = Path(BACKEND / "app" / "main.py").read_text(encoding="utf-8")
        self.assertIn("echo=settings.SQL_ECHO", database)
        self.assertIn("reload=settings.DEV_RELOAD", main)
        self.assertNotIn("echo=settings.DEBUG", database)
        self.assertNotIn("reload=settings.DEBUG", main)


class ProductionWarningTests(unittest.TestCase):
    def capture(self, **overrides):
        import logging

        from app import config

        with self.assertLogs(config._config_logger, level=logging.WARNING) as logs:
            config._config_logger.warning("(sentinel so the capture is never empty)")
            _production(**overrides)
        return " ".join(logs.output)

    def test_the_built_in_payment_link_is_warned_about_not_refused(self):
        text = self.capture()
        self.assertIn("RECHARGE_PAYMENT_URL is not set", text)

    def test_setting_your_own_link_silences_the_warning(self):
        self.assertNotIn("RECHARGE_PAYMENT_URL", self.capture(RECHARGE_PAYMENT_URL="https://rzp.io/l/mine"))

    def test_eager_celery_in_production_is_warned_about(self):
        self.assertIn("CELERY_TASK_ALWAYS_EAGER", self.capture(CELERY_TASK_ALWAYS_EAGER="true"))
        self.assertNotIn("CELERY_TASK_ALWAYS_EAGER", self.capture(CELERY_TASK_ALWAYS_EAGER="false"))

    def test_unsafe_production_config_is_still_refused(self):
        with self.assertRaises(Exception):
            _production(SECRET_KEY="short")


if __name__ == "__main__":
    unittest.main()
