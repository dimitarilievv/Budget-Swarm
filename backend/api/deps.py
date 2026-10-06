"""Authentication dependencies (auth).

Two FastAPI dependencies resolve the caller from a ``Authorization: Bearer <token>``
header, used across the protected and the public-but-personalized routes:

* :func:`get_current_user` — **required**. Missing, malformed, expired, or otherwise
  invalid tokens, and tokens whose user no longer exists, all raise
  :class:`~errors.UnauthorizedError` (→ the ``401 UNAUTHORIZED`` envelope).
* :func:`get_optional_user` — **optional**. No header → ``None`` (anonymous, used by the
  public ``generate-trip`` pipeline). A header that *is* present but invalid still raises
  401, so a broken token is never silently downgraded to anonymous.

``HTTPBearer(auto_error=False)`` is used so the scheme is advertised in the OpenAPI
security section while this module keeps control of the error envelope (FastAPI's own
default would emit a non-envelope 403).
"""

from __future__ import annotations

from config import Settings, get_settings
from db.models import User
from db.session import get_db
from errors import UnauthorizedError
from fastapi import Depends
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from services.auth import decode_access_token
from sqlalchemy.orm import Session

# Advertise the Bearer scheme in OpenAPI; we raise our own 401 rather than its 403.
_bearer_scheme = HTTPBearer(auto_error=False, description="JWT access token")


def _resolve_user(
    credentials: HTTPAuthorizationCredentials | None,
    db: Session,
    settings: Settings,
) -> User:
    if credentials is None or not credentials.credentials:
        raise UnauthorizedError("Authentication required")
    user_id = decode_access_token(credentials.credentials, settings)
    user = db.get(User, user_id)
    if user is None:
        # Token validly signed but the account is gone — treat as unauthenticated.
        raise UnauthorizedError("Invalid or expired token")
    return user


def get_current_user(
    credentials: HTTPAuthorizationCredentials | None = Depends(_bearer_scheme),
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
) -> User:
    """Resolve the authenticated user, or raise 401. Use on protected routes."""
    return _resolve_user(credentials, db, settings)


def get_optional_user(
    credentials: HTTPAuthorizationCredentials | None = Depends(_bearer_scheme),
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
) -> User | None:
    """Resolve the user if a token is supplied, else ``None``; a bad token still 401s."""
    if credentials is None or not credentials.credentials:
        return None
    return _resolve_user(credentials, db, settings)
