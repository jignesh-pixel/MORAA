"""Authentication Pydantic schemas."""

from datetime import datetime
from typing import Optional

from pydantic import BaseModel, EmailStr, Field, field_validator


class SignupRequest(BaseModel):
    """User signup request."""

    email: EmailStr = Field(..., description="User email address")
    username: str = Field(..., min_length=3, max_length=50, description="Username")
    password: str = Field(
        ..., min_length=8, max_length=128, description="User password"
    )
    full_name: Optional[str] = Field(None, max_length=255, description="Full name")

    @field_validator("username")
    @classmethod
    def validate_username(cls, v: str) -> str:
        if not v.isalnum():
            raise ValueError("Username must be alphanumeric")
        return v

    @field_validator("password")
    @classmethod
    def validate_password(cls, v: str) -> str:
        if not any(c.isupper() for c in v):
            raise ValueError("Password must contain at least one uppercase letter")
        if not any(c.isdigit() for c in v):
            raise ValueError("Password must contain at least one digit")
        if not any(c in "!@#$%^&*()_+-=[]{}|;:,.<>?" for c in v):
            raise ValueError("Password must contain at least one special character")
        return v


class LoginRequest(BaseModel):
    """User login request."""

    username: str = Field(..., description="Username")
    password: str = Field(..., description="User password")


class TokenResponse(BaseModel):
    """JWT token response."""

    access_token: str = Field(..., description="JWT access token")
    refresh_token: str = Field(..., description="JWT refresh token")
    token_type: str = Field("bearer", description="Token type")


class LoginResponse(BaseModel):
    """Login response with user info and tokens."""

    access_token: str = Field(..., description="JWT access token")
    refresh_token: str = Field(..., description="JWT refresh token")
    token_type: str = Field("bearer", description="Token type")
    user: "UserResponse"


class UserResponse(BaseModel):
    """User profile response."""

    id: str = Field(..., description="User ID")
    email: str = Field(..., description="User email")
    username: str = Field(..., description="Username")
    full_name: Optional[str] = Field(None, description="Full name")
    is_active: bool = Field(..., description="Whether user is active")
    created_at: datetime = Field(..., description="Account creation date")


class RefreshTokenRequest(BaseModel):
    """Refresh token request."""

    refresh_token: str = Field(..., description="Valid refresh token")


class GoogleLoginRequest(BaseModel):
    """The ID token Google's sign-in button gives the dashboard page."""

    id_token: str = Field(..., min_length=20, max_length=4096)
