"""Phase 4 / SEC-4: deactivated users lose access at once, refresh tokens are single-use and revocable, reuse of a
used token logs the account out everywhere, and admin-only endpoints need an administrator once one is configured."""

import time
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

from fastapi import HTTPException
from sqlalchemy.orm import sessionmaker

import app.models  # noqa: F401 -- register every model with Base.metadata
from app.api.dependencies import is_admin_user, require_admin
from app.config import settings
from app.database import Base
from app.models.revoked_token import RevokedToken
from app.models.user import User
from app.schemas.auth import SignupRequest
from app.services.auth_service import AuthService
from app.utils.security import create_access_token, create_refresh_token, decode_token
from tests.db_support import make_engine


class _AuthBase(unittest.TestCase):
    def setUp(self):
        self.engine = make_engine()
        Base.metadata.create_all(bind=self.engine)
        self.db = sessionmaker(bind=self.engine)()
        self.addCleanup(self.engine.dispose)
        self.addCleanup(self.db.close)
        self.service = AuthService(self.db)

    def signup(self, name="owner"):
        response = self.service.signup(SignupRequest(email=f"{name}@example.com", username=name,
                                                      password="Correct-Horse-Battery-9", full_name="T"))
        return response.access_token, response.refresh_token

    def user(self, name="owner"):
        return self.db.query(User).filter_by(username=name).one()


class AccessTokenTests(_AuthBase):
    def test_a_valid_access_token_finds_the_user(self):
        access, _ = self.signup()
        self.assertEqual(self.service.get_current_user(access).username, "owner")

    def test_a_deactivated_account_loses_access_immediately(self):
        access, _ = self.signup()
        user = self.user()
        user.is_active = False
        self.db.commit()
        self.assertIsNone(self.service.get_current_user(access))

    def test_tokens_issued_before_the_cutoff_are_refused_and_later_ones_work(self):
        access, _ = self.signup()
        user = self.user()
        user.tokens_valid_after = datetime.now(timezone.utc) + timedelta(seconds=1)
        self.db.commit()
        self.assertIsNone(self.service.get_current_user(access))
        time.sleep(1.2)
        fresh = create_access_token({"sub": user.id})
        self.assertEqual(self.service.get_current_user(fresh).id, user.id)

    def test_garbage_and_wrong_type_tokens_are_refused(self):
        _, refresh = self.signup()
        self.assertIsNone(self.service.get_current_user("not.a.token"))
        self.assertIsNone(self.service.get_current_user(refresh))                 # a refresh token is not an access token


class RefreshTokenTests(_AuthBase):
    def test_a_refresh_token_works_once_and_the_new_one_works(self):
        _, first = self.signup()
        second = self.service.refresh_token(first)
        self.assertTrue(second.access_token and second.refresh_token)
        third = self.service.refresh_token(second.refresh_token)                   # rotation: the chain continues
        self.assertTrue(third.refresh_token)
        self.assertEqual(self.db.query(RevokedToken).count(), 2)

    def test_reusing_a_used_refresh_token_is_refused_and_logs_the_account_out_everywhere(self):
        access, first = self.signup()
        second = self.service.refresh_token(first)
        self._age_revocations(60)                                                  # well past the retry grace window
        with self.assertRaises(ValueError) as ctx:
            self.service.refresh_token(first)                                      # the same token again: stolen copy?
        self.assertIn("already used", str(ctx.exception))
        self.db.expire_all()
        self.assertIsNotNone(self.user().tokens_valid_after)
        self.assertIsNone(self.service.get_current_user(access))                   # older access tokens are dead too
        with self.assertRaises(ValueError):
            self.service.refresh_token(second.refresh_token)                       # and so is the legitimate new refresh

    def _age_revocations(self, seconds):
        from datetime import datetime, timedelta, timezone
        for row in self.db.query(RevokedToken).all():
            row.revoked_at = datetime.now(timezone.utc) - timedelta(seconds=seconds)
        self.db.commit()

    def test_a_quick_retry_of_a_used_token_is_refused_without_logging_the_owner_out(self):
        access, first = self.signup()
        second = self.service.refresh_token(first)
        with self.assertRaises(ValueError):
            self.service.refresh_token(first)                                      # double tap / network retry
        self.db.expire_all()
        self.assertIsNone(self.user().tokens_valid_after)
        self.assertIsNotNone(self.service.get_current_user(access))
        self.assertTrue(self.service.refresh_token(second.refresh_token).access_token)

    def test_expired_revocation_rows_are_purged(self):
        from datetime import datetime, timedelta, timezone
        _, first = self.signup()
        self.service.refresh_token(first)
        for row in self.db.query(RevokedToken).all():
            row.expires_at = datetime.now(timezone.utc) - timedelta(days=1)
        self.db.commit()
        self.service.logout(self.service._generate_auth_response(self.user()).refresh_token)
        self.assertEqual(self.db.query(RevokedToken).count(), 1)

    def test_logout_revokes_the_refresh_token(self):
        _, refresh = self.signup()
        self.service.logout(refresh)
        with self.assertRaises(ValueError):
            self.service.refresh_token(refresh)

    def test_logout_never_fails_on_garbage_or_twice(self):
        self.service.logout("garbage")
        _, refresh = self.signup()
        self.service.logout(refresh)
        self.service.logout(refresh)

    def test_a_deactivated_user_cannot_refresh(self):
        _, refresh = self.signup()
        user = self.user()
        user.is_active = False
        self.db.commit()
        with self.assertRaises(ValueError):
            self.service.refresh_token(refresh)

    def test_an_access_token_cannot_be_used_to_refresh(self):
        access, _ = self.signup()
        with self.assertRaises(ValueError):
            self.service.refresh_token(access)


class AdminTests(_AuthBase):
    def test_while_no_admin_exists_everyone_logged_in_is_let_through(self):
        self.signup("alice")
        with patch.object(settings, "ADMIN_USERNAMES", ""):
            self.assertEqual(require_admin(self.user("alice"), self.db).username, "alice")

    def test_once_an_admin_is_configured_others_get_403(self):
        self.signup("alice")
        self.signup("bob")
        with patch.object(settings, "ADMIN_USERNAMES", "alice"):
            self.assertEqual(require_admin(self.user("alice"), self.db).username, "alice")
            with self.assertRaises(HTTPException) as ctx:
                require_admin(self.user("bob"), self.db)
            self.assertEqual(ctx.exception.status_code, 403)

    def test_the_database_flag_also_makes_an_admin_and_turns_the_check_on(self):
        self.signup("alice")
        self.signup("bob")
        alice = self.user("alice")
        alice.is_admin = True
        self.db.commit()
        with patch.object(settings, "ADMIN_USERNAMES", ""):
            self.assertTrue(is_admin_user(alice, self.db))
            self.assertEqual(require_admin(alice, self.db).username, "alice")
            with self.assertRaises(HTTPException):
                require_admin(self.user("bob"), self.db)

    def test_the_names_are_case_insensitive_and_trimmed(self):
        self.signup("alice")
        with patch.object(settings, "ADMIN_USERNAMES", " Alice , other "):
            self.assertTrue(is_admin_user(self.user("alice"), self.db))

    def test_the_retry_and_failure_rate_endpoints_require_an_admin(self):
        import inspect

        from app.api.routes import meta_webhook

        for fn in (meta_webhook.retry_delivery, meta_webhook.get_generation_failure_rate):
            default = inspect.signature(fn).parameters["current_user"].default
            self.assertIs(default.dependency, require_admin, fn.__name__)


if __name__ == "__main__":
    unittest.main()
