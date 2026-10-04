"""Authentication API routes."""

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session

from app.config import settings
from app.database import get_db
from app.schemas.auth import (
    GoogleLoginRequest,
    LoginRequest,
    LoginResponse,
    RefreshTokenRequest,
    SignupRequest,
    TokenResponse,
    UserResponse,
)
from app.services.auth_service import AuthService, verify_google_id_token
from app.utils.logger import logger

router = APIRouter(prefix="/api/auth", tags=["Authentication"])


@router.post(
    "/signup",
    response_model=LoginResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Register a new user",
    description="Create a new user account with email, username, and password.",
)
def signup(request: SignupRequest, db: Session = Depends(get_db)):
    """Register a new user account."""
    if not settings.ALLOW_SIGNUP:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Signup is disabled",
        )
    service = AuthService(db)
    try:
        result = service.signup(request)
        logger.info(f"User registered: {request.username}")
        return result
    except ValueError as e:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=str(e),
        )


@router.post(
    "/login",
    response_model=LoginResponse,
    summary="User login",
    description="Authenticate with username and password to receive JWT tokens.",
)
def login(request: LoginRequest, db: Session = Depends(get_db)):
    """Authenticate user and return JWT tokens."""
    service = AuthService(db)
    try:
        result = service.login(request.username, request.password)
        return result
    except ValueError as e:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=str(e),
        )


@router.post(
    "/google",
    response_model=LoginResponse,
    summary="Sign in with Google (dashboard)",
    description="Exchange a Google ID token for our tokens. Only emails in DASHBOARD_ALLOWED_EMAILS are accepted.",
)
async def google_login(request: GoogleLoginRequest, db: Session = Depends(get_db)):
    if not settings.GOOGLE_CLIENT_ID:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Google sign-in is not configured")
    email = await verify_google_id_token(request.id_token)
    allowed = {e.strip().lower() for e in (settings.DASHBOARD_ALLOWED_EMAILS or "").split(",") if e.strip()}
    if not email or email.lower() not in allowed:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="This Google account is not allowed")
    try:
        return AuthService(db).login_with_verified_email(email)
    except ValueError as e:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=str(e))


@router.post(
    "/refresh",
    response_model=TokenResponse,
    summary="Refresh access token",
    description="Get a new access token using a valid refresh token.",
)
def refresh_token(request: RefreshTokenRequest, db: Session = Depends(get_db)):
    """Refresh an expired access token."""
    service = AuthService(db)
    try:
        result = service.refresh_token(request.refresh_token)
        return result
    except ValueError as e:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=str(e),
        )


@router.post(
    "/logout",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Log out",
    description="Revoke a refresh token so it can never be used again. Always succeeds.",
)
def logout(request: RefreshTokenRequest, db: Session = Depends(get_db)):
    """Revoke the given refresh token (the short-lived access token simply expires)."""
    AuthService(db).logout(request.refresh_token)
