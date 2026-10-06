"""Password hashing + JWT / refresh-token primitives (auth).

Pure security helpers with no database or HTTP knowledge, so they are trivially
unit-testable and reused by the auth routes and the ``get_current_user`` dependency:

* **Passwords** are hashed with **bcrypt (cost 12)**; the hash is verified in constant
  time. Bcrypt only considers the first 72 bytes of a password, so both hashing and
  verification truncate to 72 bytes consistently (the wire schema additionally caps the
  length), rather than letting bcrypt raise on a longer input.
* **Access tokens** are short-lived signed JWTs carrying the user id in ``sub`` and a
  ``type: "access"`` claim, read from the ``Authorization: Bearer`` header. Decoding
  rejects a missing/invalid/expired token (and a non-access token) with
  :class:`~errors.UnauthorizedError`.
* **Refresh tokens** are opaque high-entropy strings handed to the client once; only
  their SHA-256 hash is ever stored (see :class:`db.models.RefreshToken`), so the raw
  value never touches the database and a store leak cannot be replayed.

Secret, algorithm and lifetimes come from settings (env) — never hardcoded.
"""

from __future__ import annotations

import hashlib
import secrets
from datetime import UTC, datetime, timedelta

import bcrypt
import jwt
from config import Settings
from errors import UnauthorizedError

# Bcrypt work factor (cost). 12 is the task's floor and a sane 2020s default.
_BCRYPT_ROUNDS = 12
# Bcrypt ignores bytes past this length; truncate consistently so a long password
# neither raises nor hashes inconsistently between sign-up and sign-in.
_BCRYPT_MAX_BYTES = 72
# Bytes of entropy in a raw refresh token (~64 url-safe chars).
_REFRESH_TOKEN_BYTES = 48


# --- passwords ---------------------------------------------------------------


def _password_bytes(password: str) -> bytes:
    return password.encode("utf-8")[:_BCRYPT_MAX_BYTES]


def hash_password(password: str) -> str:
    """Return a bcrypt hash (cost 12) of ``password``, safe to store."""
    return bcrypt.hashpw(_password_bytes(password), bcrypt.gensalt(rounds=_BCRYPT_ROUNDS)).decode()


def verify_password(password: str, password_hash: str) -> bool:
    """Constant-time check of ``password`` against a stored bcrypt ``password_hash``."""
    try:
        return bcrypt.checkpw(_password_bytes(password), password_hash.encode("utf-8"))
    except (ValueError, TypeError):
        # A malformed/empty stored hash must never raise — it is simply not a match.
        return False


# --- access tokens (JWT) -----------------------------------------------------


def create_access_token(user_id: int, settings: Settings, *, now: datetime | None = None) -> str:
    """Sign a short-lived access JWT for ``user_id`` (``now`` injectable for tests)."""
    issued = now or datetime.now(UTC)
    payload = {
        "sub": str(user_id),
        "type": "access",
        "iat": int(issued.timestamp()),
        "exp": issued + timedelta(minutes=settings.access_token_expire_minutes),
    }
    return jwt.encode(payload, settings.jwt_secret, algorithm=settings.jwt_algorithm)


def decode_access_token(token: str, settings: Settings) -> int:
    """Return the ``user_id`` from a valid access token, else raise ``UnauthorizedError``."""
    try:
        payload = jwt.decode(token, settings.jwt_secret, algorithms=[settings.jwt_algorithm])
    except jwt.PyJWTError as exc:
        # Covers an expired, tampered, wrong-secret or otherwise malformed token.
        raise UnauthorizedError("Invalid or expired token") from exc
    if payload.get("type") != "access":
        raise UnauthorizedError("Invalid or expired token")
    sub = payload.get("sub")
    if sub is None:
        raise UnauthorizedError("Invalid or expired token")
    try:
        return int(sub)
    except (TypeError, ValueError) as exc:
        raise UnauthorizedError("Invalid or expired token") from exc


# --- refresh tokens (opaque, stored hashed) ----------------------------------


def generate_refresh_token() -> str:
    """A fresh high-entropy opaque refresh token (handed to the client, never stored raw)."""
    return secrets.token_urlsafe(_REFRESH_TOKEN_BYTES)


def hash_refresh_token(raw_token: str) -> str:
    """The SHA-256 hex digest of a refresh token — this is what gets persisted/looked up."""
    return hashlib.sha256(raw_token.encode("utf-8")).hexdigest()


def refresh_token_expiry(settings: Settings, *, now: datetime | None = None) -> datetime:
    """Absolute expiry for a newly issued refresh token, per settings."""
    return (now or datetime.now(UTC)) + timedelta(days=settings.refresh_token_expire_days)
