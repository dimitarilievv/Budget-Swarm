"""Request/response schemas for the auth endpoints (auth).

The HTTP contract for ``/api/auth/*``. As with :mod:`schemas.trip_schema`, these
Pydantic models are the authoritative server-side validation — the browser's checks are
never trusted. Email is normalized (trimmed + lower-cased) and shape-validated here so
uniqueness is case-insensitive; passwords are length-checked (min 8). Crucially, the
user-facing :class:`UserOut` has **no** ``password_hash`` field, so a hash can never be
serialized into a response.

Email is validated with a pragmatic regex rather than ``EmailStr`` to avoid pulling in
the optional ``email-validator`` dependency; it rejects the obviously invalid while
staying permissive about the long tail of technically-legal addresses.
"""

from __future__ import annotations

import re
from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field, field_validator

# Pragmatic address shape: one @, non-empty local part, a dotted domain, no spaces.
_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")

# Password bounds: min 8 (task requirement); an upper cap keeps a pathological input
# from reaching the hasher (bcrypt only uses the first 72 bytes regardless).
_PASSWORD_MIN = 8
_PASSWORD_MAX = 128


def _normalize_email(value: str) -> str:
    email = value.strip().lower()
    if not _EMAIL_RE.match(email):
        raise ValueError("a valid email address is required")
    return email


# --- requests ----------------------------------------------------------------


class RegisterRequest(BaseModel):
    """Body for ``POST /api/auth/register``."""

    model_config = ConfigDict(extra="forbid")

    email: str = Field(description="Account email (stored lower-cased, must be unique).")
    password: str = Field(
        min_length=_PASSWORD_MIN,
        max_length=_PASSWORD_MAX,
        description="Plain password, at least 8 characters; stored only as a bcrypt hash.",
    )
    name: str | None = Field(default=None, max_length=120, description="Optional display name.")

    @field_validator("email")
    @classmethod
    def _email(cls, value: str) -> str:
        return _normalize_email(value)

    @field_validator("name")
    @classmethod
    def _name(cls, value: str | None) -> str | None:
        if value is None:
            return None
        trimmed = value.strip()
        return trimmed or None


class LoginRequest(BaseModel):
    """Body for ``POST /api/auth/login``."""

    model_config = ConfigDict(extra="forbid")

    email: str
    password: str = Field(min_length=1)

    @field_validator("email")
    @classmethod
    def _email(cls, value: str) -> str:
        # Normalize so a differently-cased email still matches the stored account; a
        # malformed address simply won't match any user (→ generic invalid-credentials).
        return value.strip().lower()


class RefreshRequest(BaseModel):
    """Body for ``POST /api/auth/refresh`` and ``POST /api/auth/logout``."""

    model_config = ConfigDict(extra="forbid")

    refresh_token: str = Field(min_length=1)


class ChangePasswordRequest(BaseModel):
    """Body for ``POST /api/auth/change-password``."""

    model_config = ConfigDict(extra="forbid")

    current_password: str = Field(min_length=1, description="The account's current password.")
    new_password: str = Field(
        min_length=_PASSWORD_MIN,
        max_length=_PASSWORD_MAX,
        description="New plain password, at least 8 characters; stored only as a bcrypt hash.",
    )


# --- responses ---------------------------------------------------------------


class UserOut(BaseModel):
    """Public user representation — deliberately without ``password_hash``."""

    model_config = ConfigDict(from_attributes=True)

    id: int
    email: str
    name: str | None = None
    created_at: datetime
    updated_at: datetime


class TokenPair(BaseModel):
    """An access token (+ its lifetime) and a refresh token."""

    access_token: str
    refresh_token: str
    token_type: str = "bearer"
    expires_in: int = Field(description="Access-token lifetime in seconds.")


class AuthResponse(BaseModel):
    """``register`` / ``login`` response: the user plus a fresh token pair."""

    user: UserOut
    tokens: TokenPair
