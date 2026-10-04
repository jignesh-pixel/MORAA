"""Security utilities: password hashing, JWT token creation/verification."""

import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Optional

import bcrypt
from jose import JWTError, jwt

from app.config import settings

# bcrypt only reads the first 72 bytes of a password. passlib (used before)
# truncated silently; bcrypt>=5 raises instead. Truncating the same way keeps
# every existing $2b$ hash verifiable and long passwords usable.
_BCRYPT_MAX_BYTES = 72


def _password_bytes(password: str) -> bytes:
    return password.encode("utf-8")[:_BCRYPT_MAX_BYTES]


def hash_password(password: str) -> str:
    """Hash a plain text password (bcrypt, $2b$ format)."""
    return bcrypt.hashpw(_password_bytes(password), bcrypt.gensalt()).decode("ascii")


def verify_password(plain_password: str, hashed_password: str) -> bool:
    """Verify a plain text password against a bcrypt hash; False on any malformed input."""
    if not plain_password or not hashed_password:
        return False
    try:
        return bcrypt.checkpw(_password_bytes(plain_password), hashed_password.encode("ascii"))
    except (ValueError, TypeError, UnicodeEncodeError):
        return False


def create_access_token(
    data: Dict[str, Any], expires_delta: Optional[timedelta] = None
) -> str:
    """Create a JWT access token."""
    to_encode = data.copy()
    now = datetime.now(timezone.utc)
    expire = now + (expires_delta or timedelta(minutes=settings.ACCESS_TOKEN_EXPIRE_MINUTES))
    to_encode.update({"exp": expire, "iat": now, "type": "access"})
    return jwt.encode(to_encode, settings.SECRET_KEY, algorithm=settings.ALGORITHM)


def create_refresh_token(
    data: Dict[str, Any], expires_delta: Optional[timedelta] = None
) -> str:
    """Create a JWT refresh token."""
    to_encode = data.copy()
    now = datetime.now(timezone.utc)
    expire = now + (expires_delta or timedelta(days=settings.REFRESH_TOKEN_EXPIRE_DAYS))
    to_encode.update({"exp": expire, "iat": now, "type": "refresh", "jti": str(uuid.uuid4())})
    return jwt.encode(to_encode, settings.SECRET_KEY, algorithm=settings.ALGORITHM)


def decode_token(token: str) -> Optional[Dict[str, Any]]:
    """Decode and validate a JWT token."""
    try:
        payload = jwt.decode(
            token, settings.SECRET_KEY, algorithms=[settings.ALGORITHM]
        )
        return payload
    except JWTError:
        return None


def verify_token(token: str, expected_type: str = "access") -> Optional[Dict[str, Any]]:
    """Verify token and check its type."""
    payload = decode_token(token)
    if payload is None:
        return None
    if payload.get("type") != expected_type:
        return None
    return payload
