"""Phase 0 / U1: config loads from an absolute path and refuses unsafe production boots."""

import unittest
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
        with patch.dict("os.environ", {"MORAA_ENV_FILE": "/tmp/test.env"}):
            self.assertEqual(config_module._resolve_env_file(), "/tmp/test.env")


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
