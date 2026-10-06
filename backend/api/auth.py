"""Authentication routes under ``/api/auth`` (auth).

Implements registration, login, token refresh (with rotation), logout (revocation) and
``/me``. Security properties enforced here:

* Passwords are only ever stored/verified as bcrypt hashes (``services.auth``); the
  response schema (:class:`~schemas.auth_schema.UserOut`) has no hash field.
* Login returns one generic *"Invalid email or password"* for both an unknown email and
  a wrong password, and spends a bcrypt verification even when the email is unknown, so a
  caller cannot distinguish the two by response content or timing (user-enumeration
  defense).
* Refresh tokens are opaque, stored **hashed**, single-use and **rotated**: a successful
  refresh revokes the presented token and issues a fresh pair, so a leaked-then-rotated
  token is dead. Logout revokes the presented token and is idempotent (always 204).
* ``/login`` and ``/register`` sit behind the stricter per-IP auth rate limiter.

Secrets, token lifetimes and the hash are never logged.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from functools import lru_cache

from config import Settings, get_settings
from db.models import RefreshToken, User
from db.repositories import RefreshTokenRepository, UserRepository
from db.session import get_db
from errors import ConflictError, UnauthorizedError
from fastapi import APIRouter, Depends, Response, status
from schemas.auth_schema import (
    AuthResponse,
    LoginRequest,
    RefreshRequest,
    RegisterRequest,
    TokenPair,
    UserOut,
)
from services.auth import (
    create_access_token,
    generate_refresh_token,
    hash_password,
    hash_refresh_token,
    refresh_token_expiry,
    verify_password,
)
from sqlalchemy.orm import Session

from api.deps import get_current_user
from api.rate_limit import enforce_auth_rate_limit

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/auth", tags=["auth"])


@lru_cache
def _dummy_password_hash() -> str:
    """A throwaway bcrypt hash used to equalize login timing for unknown emails.

    Verifying the submitted password against this (always-failing) hash makes the
    "no such user" path spend the same bcrypt work as the "wrong password" path, so
    response timing does not leak whether an email is registered.
    """
    return hash_password(generate_refresh_token())


def _issue_token_pair(user: User, db: Session, settings: Settings) -> TokenPair:
    """Mint a new access token and a fresh (hashed, persisted) refresh token for a user."""
    now = datetime.now(UTC)
    access_token = create_access_token(user.id, settings, now=now)
    raw_refresh = generate_refresh_token()
    db.add(
        RefreshToken(
            user_id=user.id,
            token_hash=hash_refresh_token(raw_refresh),
            expires_at=refresh_token_expiry(settings, now=now),
        )
    )
    db.flush()
    return TokenPair(
        access_token=access_token,
        refresh_token=raw_refresh,
        expires_in=settings.access_token_expire_minutes * 60,
    )


@router.post(
    "/register",
    response_model=AuthResponse,
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(enforce_auth_rate_limit)],
)
def register(
    body: RegisterRequest,
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
) -> AuthResponse:
    """Create an account and return the user (without the hash) plus a token pair."""
    users = UserRepository(db)
    if users.get_by_email(body.email) is not None:
        raise ConflictError("An account with this email already exists", field="email")

    user = User(
        email=body.email,
        password_hash=hash_password(body.password),
        name=body.name,
    )
    db.add(user)
    db.flush()
    db.refresh(user)  # load server-side created_at/updated_at for the response
    tokens = _issue_token_pair(user, db, settings)
    db.commit()
    logger.info("auth: registered user id=%s", user.id)
    return AuthResponse(user=UserOut.model_validate(user), tokens=tokens)


@router.post(
    "/login",
    response_model=AuthResponse,
    dependencies=[Depends(enforce_auth_rate_limit)],
)
def login(
    body: LoginRequest,
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
) -> AuthResponse:
    """Verify credentials and return the user plus a fresh token pair."""
    user = UserRepository(db).get_by_email(body.email)
    if user is None:
        # Spend equivalent bcrypt work so timing doesn't reveal that the email is unknown.
        verify_password(body.password, _dummy_password_hash())
        raise UnauthorizedError("Invalid email or password")
    if not verify_password(body.password, user.password_hash):
        raise UnauthorizedError("Invalid email or password")

    tokens = _issue_token_pair(user, db, settings)
    db.commit()
    logger.info("auth: login user id=%s", user.id)
    return AuthResponse(user=UserOut.model_validate(user), tokens=tokens)


@router.post("/refresh", response_model=TokenPair)
def refresh(
    body: RefreshRequest,
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
) -> TokenPair:
    """Exchange a valid refresh token for a new access token, rotating the refresh token."""
    now = datetime.now(UTC)
    tokens_repo = RefreshTokenRepository(db)
    existing = tokens_repo.get_active_by_hash(hash_refresh_token(body.refresh_token), now=now)
    if existing is None:
        raise UnauthorizedError("Invalid or expired refresh token")

    user = db.get(User, existing.user_id)
    if user is None:
        raise UnauthorizedError("Invalid or expired refresh token")

    # Rotate: the presented token is single-use — revoke it and mint a brand-new pair.
    tokens_repo.revoke(existing, now=now)
    tokens = _issue_token_pair(user, db, settings)
    db.commit()
    logger.info("auth: refreshed tokens for user id=%s", user.id)
    return tokens


@router.post("/logout", status_code=status.HTTP_204_NO_CONTENT)
def logout(
    body: RefreshRequest,
    db: Session = Depends(get_db),
) -> Response:
    """Invalidate a refresh token. Idempotent: an unknown/already-revoked token still 204s."""
    now = datetime.now(UTC)
    tokens_repo = RefreshTokenRepository(db)
    existing = tokens_repo.get_active_by_hash(hash_refresh_token(body.refresh_token), now=now)
    if existing is not None:
        tokens_repo.revoke(existing, now=now)
        db.commit()
        logger.info("auth: logout revoked a refresh token for user id=%s", existing.user_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.get("/me", response_model=UserOut)
def me(user: User = Depends(get_current_user)) -> UserOut:
    """Return the currently authenticated user."""
    return UserOut.model_validate(user)
