"""FastAPI dependencies for authentication and database sessions."""

from typing import Optional

from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy.orm import Session

from app.config import settings
from app.database import get_db
from app.models.user import User
from app.services.auth_service import AuthService
from app.utils.logger import logger

security = HTTPBearer(auto_error=False)


def get_current_user(
    credentials: Optional[HTTPAuthorizationCredentials] = Depends(security),
    db: Session = Depends(get_db),
) -> Optional[User]:
    """
    Get the current authenticated user from JWT token.
    Returns None if no token provided (optional auth).
    """
    if credentials is None:
        return None

    auth_service = AuthService(db)
    user = auth_service.get_current_user(credentials.credentials)
    return user


def require_auth(
    credentials: Optional[HTTPAuthorizationCredentials] = Depends(security),
    db: Session = Depends(get_db),
) -> User:
    """
    Require authentication - raises 401 if no valid token.
    """
    if credentials is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Authentication required",
            headers={"WWW-Authenticate": "Bearer"},
        )

    auth_service = AuthService(db)
    user = auth_service.get_current_user(credentials.credentials)

    if user is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or expired token",
            headers={"WWW-Authenticate": "Bearer"},
        )

    return user


def _admin_usernames() -> set:
    return {n.strip().lower() for n in (settings.ADMIN_USERNAMES or "").split(",") if n.strip()}


def is_admin_user(user: User, db: Session) -> bool:
    return bool(getattr(user, "is_admin", False)) or str(getattr(user, "username", "") or "").lower() in _admin_usernames()


def require_admin(
    user: User = Depends(require_auth),
    db: Session = Depends(get_db),
) -> User:
    """Require an administrator (is_admin in the database, or listed in ADMIN_USERNAMES). 403 otherwise.

    Compatibility: while NO administrator exists anywhere (nobody flagged, ADMIN_USERNAMES empty) every logged-in
    user is let through, exactly as before, with a warning in the log, so adding this check cannot lock the
    owner out of the retry endpoint. Configure an administrator to turn the check on."""
    if is_admin_user(user, db):
        return user
    no_admin_configured = not _admin_usernames() and db.query(User.id).filter(User.is_admin.is_(True)).first() is None
    if no_admin_configured:
        logger.warning(
            "Admin-only endpoint used while no administrator is configured; allowing any logged-in user. "
            "Set ADMIN_USERNAMES (or flag a user is_admin) to restrict it."
        )
        return user
    raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Administrator access required")
