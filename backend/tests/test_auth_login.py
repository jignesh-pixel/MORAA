"""Phase 0 / U6 (SEC-2): password hashing works with bcrypt 5 and login issues usable tokens.

passlib 1.7.4 + bcrypt 5 raised ValueError on every hash/verify, so every
login returned 401 and the admin-only endpoints were unreachable.
"""

import unittest

import bcrypt
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import app.models  # noqa: F401 -- register every model with Base.metadata
from app.database import Base, get_db
from app.main import app
from app.models.user import User
from app.utils.security import hash_password, verify_password

PASSWORD = "Correct-Horse-Battery-Staple-42"


class PasswordHashingTests(unittest.TestCase):
    def test_round_trip(self):
        hashed = hash_password(PASSWORD)
        self.assertTrue(hashed.startswith("$2b$"))
        self.assertTrue(verify_password(PASSWORD, hashed))
        self.assertFalse(verify_password(PASSWORD + "x", hashed))

    def test_hashes_are_salted(self):
        self.assertNotEqual(hash_password(PASSWORD), hash_password(PASSWORD))

    def test_long_password_truncated_like_passlib(self):
        long_pw = "é" * 50  # 100 UTF-8 bytes, beyond bcrypt's 72-byte limit
        hashed = hash_password(long_pw)
        self.assertTrue(verify_password(long_pw, hashed))
        # passlib truncated silently at 72 bytes; same first 72 bytes verify.
        self.assertTrue(verify_password("é" * 36 + "different-tail", hashed))

    def test_existing_hashes_from_before_still_verify(self):
        # Hashes produced by passlib's bcrypt backend are plain $2b$ / $2a$
        # strings; produce them the same way and check they verify.
        for prefix in (b"2b", b"2a"):
            legacy = bcrypt.hashpw(PASSWORD.encode(), bcrypt.gensalt(rounds=4, prefix=prefix)).decode()
            self.assertTrue(verify_password(PASSWORD, legacy), prefix)

    def test_malformed_input_is_false_not_an_error(self):
        for hashed in ("", "not-a-hash", "$2b$12$short", "$argon2id$v=19$x"):
            self.assertFalse(verify_password(PASSWORD, hashed), hashed)
        self.assertFalse(verify_password("", hash_password(PASSWORD)))


class LoginEndToEndTests(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine(
            "sqlite:///:memory:", connect_args={"check_same_thread": False}, poolclass=StaticPool
        )
        Base.metadata.create_all(bind=self.engine)
        self.db = sessionmaker(bind=self.engine)()
        self.db.add(User(email="ops@example.com", username="ops", hashed_password=hash_password(PASSWORD),
                         is_active=True))
        self.db.add(User(email="gone@example.com", username="gone", hashed_password=hash_password(PASSWORD),
                         is_active=False))
        self.db.commit()

        def _db():
            yield self.db

        app.dependency_overrides[get_db] = _db
        self.client = TestClient(app)

    def tearDown(self):
        app.dependency_overrides.pop(get_db, None)
        self.db.close()
        self.engine.dispose()

    def _login(self, username, password):
        return self.client.post("/api/auth/login", json={"username": username, "password": password},
                                headers={"host": "localhost"})

    def test_login_returns_token_that_opens_an_admin_route(self):
        r = self._login("ops", PASSWORD)
        self.assertEqual(r.status_code, 200, r.text)
        body = r.json()
        token = body.get("access_token") or (body.get("tokens") or {}).get("access_token")
        self.assertTrue(token, body)

        denied = self.client.get("/api/meta/webhook/failure-rate", headers={"host": "localhost"})
        self.assertEqual(denied.status_code, 401)
        allowed = self.client.get("/api/meta/webhook/failure-rate",
                                  headers={"host": "localhost", "Authorization": f"Bearer {token}"})
        self.assertNotIn(allowed.status_code, (401, 403), allowed.text)

    def test_wrong_password_and_inactive_user_rejected(self):
        self.assertEqual(self._login("ops", "wrong").status_code, 401)
        self.assertEqual(self._login("nobody", PASSWORD).status_code, 401)
        self.assertEqual(self._login("gone", PASSWORD).status_code, 401)


if __name__ == "__main__":
    unittest.main()
