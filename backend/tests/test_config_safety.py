"""Phase 0 / U1: config loads from an absolute path and refuses unsafe production boots."""

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from pydantic import ValidationError

from app import config as config_module
from app.config import BASE_DIR, Settings

_SAFE_PRODUCTION = {
    "ENVIRONMENT": "production",
    "SECRET_KEY": "k" * 48,
    "META_APP_SECRET": "meta-secret",
    "RAZORPAY_WEBHOOK_SECRET": "rzp-secret",
    "DATABASE_URL": "postgresql://user:pass@db.example:5432/app",
    "DEBUG": False,
    "ALLOW_UNSIGNED_WEBHOOKS": False,
}


def _settings(**overrides):
    return Settings(_env_file=None, **{**_SAFE_PRODUCTION, **overrides})


class SafeDefaultsTests(unittest.TestCase):
    def test_defaults_never_relax_security(self):
        s = Settings(_env_file=None)
        self.assertEqual(s.ENVIRONMENT, "development")
        self.assertFalse(s.DEBUG)
        self.assertFalse(s.ALLOW_UNSIGNED_WEBHOOKS)
        self.assertEqual(s.HOST, "127.0.0.1")
        self.assertEqual(s.LOCAL_PEER_ADDRESSES, "127.0.0.1,::1")

    def test_relative_log_dir_resolves_under_backend(self):
        s = Settings(_env_file=None, LOG_DIR="logs")
        self.assertEqual(s.LOG_PATH, BASE_DIR / "logs")

    def test_absolute_log_dir_kept(self):
        target = (BASE_DIR / "some" / "where").resolve()
        s = Settings(_env_file=None, LOG_DIR=str(target))
        self.assertEqual(s.LOG_PATH, target)


class EnvFileResolutionTests(unittest.TestCase):
    def test_default_is_absolute_backend_env(self):
        with patch.dict("os.environ", {}, clear=False) as env:
            env.pop("MORAA_ENV_FILE", None)
            self.assertEqual(config_module._resolve_env_file(), str(BASE_DIR / ".env"))

    def test_empty_override_disables_env_file(self):
        with patch.dict("os.environ", {"MORAA_ENV_FILE": ""}):
            self.assertIsNone(config_module._resolve_env_file())

    def test_explicit_override_used(self):
        with tempfile.NamedTemporaryFile("w", suffix=".env", delete=False) as fh:
            fh.write("ENVIRONMENT=development\n")
        try:
            with patch.dict("os.environ", {"MORAA_ENV_FILE": fh.name}):
                self.assertEqual(config_module._resolve_env_file(), str(Path(fh.name)))
        finally:
            os.remove(fh.name)

    def test_missing_override_file_is_an_error_not_silent_defaults(self):
        with patch.dict("os.environ", {"MORAA_ENV_FILE": "definitely-missing.env"}):
            with self.assertRaises(RuntimeError) as ctx:
                config_module._resolve_env_file()
        # Relative override resolves against backend/, not the working directory.
        self.assertIn(str(BASE_DIR / "definitely-missing.env"), str(ctx.exception))

    def test_test_suite_never_loads_the_live_env_file(self):
        self.assertIsNone(config_module.ENV_FILE)

    def test_environment_loaded_from_env_file(self):
        with tempfile.NamedTemporaryFile("w", suffix=".env", delete=False) as fh:
            fh.write("ENVIRONMENT=production\nSECRET_KEY=" + "s" * 40 + "\n"
                     "META_APP_SECRET=m\nRAZORPAY_WEBHOOK_SECRET=r\n"
                     "DATABASE_URL=postgresql://u:p@h:5432/d\nDEBUG=true\nDEBUG=false\n")
        try:
            # conftest exports a test DATABASE_URL; process env outranks the file.
            with patch.dict("os.environ", {}) as env:
                env.pop("DATABASE_URL", None)
                s = Settings(_env_file=fh.name)
            self.assertTrue(s.IS_PRODUCTION)
            self.assertFalse(s.DEBUG)  # duplicate keys: the last one wins
        finally:
            os.remove(fh.name)


class ProductionGuardTests(unittest.TestCase):
    def test_safe_production_config_boots(self):
        s = _settings()
        self.assertTrue(s.IS_PRODUCTION)
        self.assertEqual(s.SECRET_KEY, "k" * 48)

    def test_each_unsafe_setting_refuses_boot(self):
        cases = {
            "SECRET_KEY is not set": {"SECRET_KEY": None},
            "META_APP_SECRET is not set": {"META_APP_SECRET": ""},
            "RAZORPAY_WEBHOOK_SECRET is not set": {"RAZORPAY_WEBHOOK_SECRET": "  "},
            "DATABASE_URL points at SQLite": {"DATABASE_URL": "sqlite:///./x.db"},
            "DEBUG is true": {"DEBUG": True},
            "ALLOW_UNSIGNED_WEBHOOKS is true": {"ALLOW_UNSIGNED_WEBHOOKS": True},
        }
        for message, override in cases.items():
            with self.subTest(message):
                with self.assertRaises(ValidationError) as ctx:
                    _settings(**override)
                self.assertIn(message, str(ctx.exception))

    def test_short_production_secret_key_refused(self):
        with self.assertRaises(ValidationError) as ctx:
            _settings(SECRET_KEY="short")
        self.assertIn("shorter than 32", str(ctx.exception))

    def test_refusal_never_echoes_secret_values(self):
        with self.assertRaises(ValidationError) as ctx:
            _settings(META_APP_SECRET="CANARY-META-VALUE", RAZORPAY_WEBHOOK_SECRET="",
                      SECRET_KEY="CANARY-SECRET-KEY-" + "x" * 30)
        text = str(ctx.exception)
        self.assertNotIn("CANARY", text)
        self.assertIn("RAZORPAY_WEBHOOK_SECRET is not set", text)

    def test_missing_production_secret_key_is_not_silently_generated(self):
        with self.assertRaises(ValidationError):
            _settings(SECRET_KEY="")

    def test_development_still_generates_secret_key(self):
        s = Settings(_env_file=None, ENVIRONMENT="development", SECRET_KEY=None)
        self.assertTrue(s.SECRET_KEY)

    def test_unknown_environment_rejected(self):
        with self.assertRaises(ValidationError):
            Settings(_env_file=None, ENVIRONMENT="staging-ish")


if __name__ == "__main__":
    unittest.main()
