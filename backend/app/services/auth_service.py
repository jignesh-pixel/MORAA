"""Authentication service for user management."""

from datetime import datetime, timedelta, timezone
from typing import Optional

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.config import settings
from app.models.revoked_token import RevokedToken
from app.models.user import User
from app.repositories.base import BaseRepository
from app.schemas.auth import LoginResponse, SignupRequest, TokenResponse, UserResponse
from app.utils.security import (
    create_access_token,
    create_refresh_token,
    decode_token,
    hash_password,
    verify_password,
)
from app.utils.logger import logger

# Hash of a throw-away password, verified when the username is unknown (see login).
REUSE_GRACE_SECONDS = 20
_DUMMY_PASSWORD_HASH = hash_password("not-a-real-password")


async def verify_google_id_token(id_token: str) -> Optional[str]:
    """Ask Google whether this ID token is genuine, for OUR client id, and return its verified email (else None)."""
    import httpx

    try:
        async with httpx.AsyncClient(timeout=8.0) as client:
            response = await client.get("https://oauth2.googleapis.com/tokeninfo", params={"id_token": id_token})
        if response.status_code != 200:
            return None
        data = response.json()
    except Exception:  # noqa: BLE001
        return None
    if data.get("aud") != settings.GOOGLE_CLIENT_ID or str(data.get("email_verified")).lower() != "true":
        return None
    return str(data.get("email") or "") or None


class AuthService:
    """Authentication and user management service."""

    def login_with_verified_email(self, email: str) -> LoginResponse:
        """Log in (creating the account on first use) a person whose email Google has verified and who is allowed in."""
        import secrets

        user = self.repo.find_first(email=email)
        if user is None:
            user = self.repo.create(
                email=email, username=email, hashed_password=hash_password(secrets.token_urlsafe(32)), full_name=email,
            )
            logger.bind(category="auth").info("Dashboard account created through Google sign-in")
        if not user.is_active:
            raise ValueError("Account is deactivated")
        return self._generate_auth_response(user)

    def __init__(self, db: Session):
        self.db = db
        self.repo = BaseRepository(User, db)

    def signup(self, request: SignupRequest) -> LoginResponse:
        """Register a new user."""
        # Check if email already exists
        existing_email = self.repo.find_first(email=request.email)
        if existing_email:
            raise ValueError("Email already registered")

        # Administrators are matched case-insensitively, so nobody may register a name that differs from an
        # administrator's only by letter case.
        from app.api.dependencies import _admin_usernames

        if request.username.strip().lower() in _admin_usernames():
            raise ValueError("Username already taken")

        # Check if username already exists
        existing_username = self.repo.find_first(username=request.username)
        if existing_username:
            raise ValueError("Username already taken")

        # Create user
        user = self.repo.create(
            email=request.email,
            username=request.username,
            hashed_password=hash_password(request.password),
            full_name=request.full_name,
        )

        logger.bind(category="auth").info(f"New user registered: {user.username} ({user.email})")

        # Generate tokens
        return self._generate_auth_response(user)

    def login(self, username: str, password: str) -> LoginResponse:
        """Authenticate user and return tokens."""
        user = self.repo.find_first(username=username)
        if not user:
            # Spend the same bcrypt time as a real check so response time does
            # not reveal whether the username exists.
            verify_password(password, _DUMMY_PASSWORD_HASH)
            raise ValueError("Invalid username or password")

        if not verify_password(password, user.hashed_password):
            raise ValueError("Invalid username or password")

        if not user.is_active:
            raise ValueError("Account is deactivated")

        logger.bind(category="auth").info(f"User logged in: {user.username}")

        return self._generate_auth_response(user)

    def refresh_token(self, refresh_token: str) -> TokenResponse:
        """Generate new access token from refresh token."""
        payload = decode_token(refresh_token)
        if payload is None:
            raise ValueError("Invalid or expired refresh token")
        if payload.get("type") != "refresh":
            raise ValueError("Invalid token type")

        user_id = payload.get("sub")
        if not user_id:
            raise ValueError("Invalid token payload")

        user = self.repo.get(user_id)
        if not user or not user.is_active:
            raise ValueError("User not found or inactive")
        if self._issued_before_cutoff(user, payload):
            raise ValueError("Invalid or expired refresh token")

        # Single-use refresh tokens (SEC-4): a token that was already used is a sign a copy exists elsewhere.
        # Refuse it and refuse every token issued to this user so far; the owner simply logs in again.
        jti = str(payload.get("jti") or "")
        used = self.db.get(RevokedToken, jti) if jti else None
        if used is not None:
            # A refused request that was merely retried (double tap, network retry) a moment after the first use is
            # not theft: refuse it, but do not log the owner out everywhere. A token the owner logged out with is
            # simply refused.
            used_at = used.revoked_at
            if used_at is not None and used_at.tzinfo is None:
                used_at = used_at.replace(tzinfo=timezone.utc)
            recent = used_at is not None and (datetime.now(timezone.utc) - used_at).total_seconds() <= REUSE_GRACE_SECONDS
            if recent:
                raise ValueError("Refresh token already used")
            user.tokens_valid_after = datetime.now(timezone.utc) + timedelta(seconds=1)
            self.db.commit()
            logger.bind(category="auth").warning(
                f"Refresh token reuse detected for user {user.username}: all earlier tokens revoked"
            )
            raise ValueError("Refresh token already used")
        if jti:
            self._revoke_jti(jti, user.id, payload)

        # Generate new tokens
        access_token = create_access_token(
            data={"sub": user.id},
            expires_delta=timedelta(minutes=settings.ACCESS_TOKEN_EXPIRE_MINUTES),
        )
        new_refresh_token = create_refresh_token(
            data={"sub": user.id},
            expires_delta=timedelta(days=settings.REFRESH_TOKEN_EXPIRE_DAYS),
        )

        return TokenResponse(
            access_token=access_token,
            refresh_token=new_refresh_token,
        )

    def get_current_user(self, token: str) -> Optional[User]:
        """Get current user from access token (None for an unknown, deactivated or logged-out-everywhere user)."""
        from app.utils.security import verify_token

        payload = verify_token(token, expected_type="access")
        if payload is None:
            return None

        user_id = payload.get("sub")
        if not user_id:
            return None

        user = self.repo.get(user_id)
        if user is None or not user.is_active:
            return None          # a deactivated account's still-valid tokens stop working at once
        if self._issued_before_cutoff(user, payload):
            return None
        return user

    @staticmethod
    def _issued_before_cutoff(user: User, payload: dict) -> bool:
        cutoff = user.tokens_valid_after
        if cutoff is None:
            return False
        if cutoff.tzinfo is None:
            cutoff = cutoff.replace(tzinfo=timezone.utc)
        # Token timestamps (iat) are whole seconds, so compare at whole-second resolution: a token issued in
        # the cut-off's own second after the cut-off must not be refused.
        return int(payload.get("iat") or 0) < int(cutoff.timestamp())

    def _revoke_jti(self, jti: str, user_id: str, payload: dict) -> None:
        expires = payload.get("exp")
        try:
            # Housekeeping: rows for tokens that have expired anyway can never matter again.
            self.db.query(RevokedToken).filter(RevokedToken.expires_at < datetime.now(timezone.utc)).delete(
                synchronize_session=False
            )
            self.db.add(RevokedToken(
                jti=jti[:64], user_id=user_id,
                expires_at=datetime.fromtimestamp(float(expires), tz=timezone.utc) if expires else None,
            ))
            self.db.commit()
        except IntegrityError:
            self.db.rollback()
            raise ValueError("Refresh token already used")

    def logout(self, refresh_token: str) -> None:
        """Revoke a refresh token so it can never be used again (the access token simply expires)."""
        payload = decode_token(refresh_token)
        if payload is None or payload.get("type") != "refresh" or not payload.get("jti") or not payload.get("sub"):
            return
        if self.db.get(RevokedToken, str(payload["jti"])) is None:
            try:
                self._revoke_jti(str(payload["jti"]), str(payload["sub"]), payload)
            except ValueError:
                pass

    def get_user_profile(self, user_id: str) -> Optional[UserResponse]:
        """Get user profile by ID."""
        user = self.repo.get(user_id)
        if not user:
            return None

        return UserResponse(
            id=user.id,
            email=user.email,
            username=user.username,
            full_name=user.full_name,
            is_active=user.is_active,
            created_at=user.created_at,
        )

    def _generate_auth_response(self, user: User) -> LoginResponse:
        """Generate authentication response with tokens."""
        access_token = create_access_token(
            data={"sub": user.id},
            expires_delta=timedelta(minutes=settings.ACCESS_TOKEN_EXPIRE_MINUTES),
        )
        refresh_token = create_refresh_token(
            data={"sub": user.id},
            expires_delta=timedelta(days=settings.REFRESH_TOKEN_EXPIRE_DAYS),
        )

        return LoginResponse(
            access_token=access_token,
            refresh_token=refresh_token,
            user=UserResponse(
                id=user.id,
                email=user.email,
                username=user.username,
                full_name=user.full_name,
                is_active=user.is_active,
                created_at=user.created_at,
            ),
        )
