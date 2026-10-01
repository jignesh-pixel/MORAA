"""Authentication service for user management."""

from datetime import timedelta, timezone
from typing import Optional

from sqlalchemy.orm import Session

from app.config import settings
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
_DUMMY_PASSWORD_HASH = hash_password("not-a-real-password")


class AuthService:
    """Authentication and user management service."""

    def __init__(self, db: Session):
        self.db = db
        self.repo = BaseRepository(User, db)

    def signup(self, request: SignupRequest) -> LoginResponse:
        """Register a new user."""
        # Check if email already exists
        existing_email = self.repo.find_first(email=request.email)
        if existing_email:
            raise ValueError("Email already registered")

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
        """Get current user from access token."""
        from app.utils.security import verify_token

        payload = verify_token(token, expected_type="access")
        if payload is None:
            return None

        user_id = payload.get("sub")
        if not user_id:
            return None

        return self.repo.get(user_id)

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
