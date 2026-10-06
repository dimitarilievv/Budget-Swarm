"""Unit tests for the auth primitives + schemas (auth) — no database.

Covers password hashing/verification (including bcrypt's 72-byte edge and a malformed
stored hash), access-token signing/decoding (round-trip, expiry, tampering, wrong type),
refresh-token generation/hashing, and the request-schema validation (email normalization,
invalid email, short password, forbidden extra fields).
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from config import Settings
from errors import UnauthorizedError
from pydantic import ValidationError
from schemas.auth_schema import LoginRequest, RegisterRequest
from services.auth import (
    create_access_token,
    decode_access_token,
    generate_refresh_token,
    hash_password,
    hash_refresh_token,
    verify_password,
)


def _settings(**overrides) -> Settings:
    base = {"database_url": "postgresql+psycopg://t:t@localhost/t", "jwt_secret": "unit-secret"}
    base.update(overrides)
    return Settings(_env_file=None, **base)


# --- passwords ---------------------------------------------------------------


def test_hash_password_is_not_plaintext_and_verifies():
    hashed = hash_password("correct horse battery")
    assert hashed != "correct horse battery"
    assert hashed.startswith("$2")  # bcrypt marker
    assert verify_password("correct horse battery", hashed) is True


def test_verify_password_rejects_wrong_password():
    hashed = hash_password("password123")
    assert verify_password("password124", hashed) is False


def test_hash_cost_is_12():
    # bcrypt hash format: $2b$<rounds>$<salt+digest>
    assert hash_password("whatever").split("$")[2] == "12"


def test_long_password_over_72_bytes_does_not_raise():
    long_pw = "a" * 200
    hashed = hash_password(long_pw)
    assert verify_password(long_pw, hashed) is True


def test_verify_password_with_malformed_hash_returns_false():
    assert verify_password("x", "not-a-bcrypt-hash") is False
    assert verify_password("x", "") is False


# --- access tokens -----------------------------------------------------------


def test_access_token_round_trips_user_id():
    settings = _settings()
    token = create_access_token(42, settings)
    assert decode_access_token(token, settings) == 42


def test_expired_access_token_is_rejected():
    settings = _settings(access_token_expire_minutes=15)
    stale = create_access_token(1, settings, now=datetime.now(UTC) - timedelta(hours=1))
    with pytest.raises(UnauthorizedError):
        decode_access_token(stale, settings)


def test_token_signed_with_other_secret_is_rejected():
    token = create_access_token(1, _settings(jwt_secret="secret-a"))
    with pytest.raises(UnauthorizedError):
        decode_access_token(token, _settings(jwt_secret="secret-b"))


def test_tampered_token_is_rejected():
    settings = _settings()
    token = create_access_token(1, settings)
    with pytest.raises(UnauthorizedError):
        decode_access_token(token + "x", settings)


def test_non_access_token_type_is_rejected():
    import jwt

    settings = _settings()
    # A validly signed token but with the wrong 'type' claim must not authenticate.
    forged = jwt.encode(
        {"sub": "1", "type": "refresh", "exp": datetime.now(UTC) + timedelta(minutes=5)},
        settings.jwt_secret,
        algorithm=settings.jwt_algorithm,
    )
    with pytest.raises(UnauthorizedError):
        decode_access_token(forged, settings)


# --- refresh tokens ----------------------------------------------------------


def test_refresh_tokens_are_unique_and_hash_is_deterministic():
    a, b = generate_refresh_token(), generate_refresh_token()
    assert a != b
    assert hash_refresh_token(a) == hash_refresh_token(a)
    assert hash_refresh_token(a) != hash_refresh_token(b)
    assert len(hash_refresh_token(a)) == 64  # sha256 hex


# --- schema validation -------------------------------------------------------


def test_register_normalizes_email_and_name():
    body = RegisterRequest(email="  USER@Example.COM ", password="password123", name="  Ann ")
    assert body.email == "user@example.com"
    assert body.name == "Ann"


@pytest.mark.parametrize("email", ["not-an-email", "a@b", "@example.com", "foo@bar."])
def test_register_rejects_invalid_email(email):
    with pytest.raises(ValidationError):
        RegisterRequest(email=email, password="password123")


def test_register_rejects_short_password():
    with pytest.raises(ValidationError):
        RegisterRequest(email="user@example.com", password="short")


def test_register_forbids_unknown_fields():
    with pytest.raises(ValidationError):
        RegisterRequest(email="user@example.com", password="password123", user_id=5)


def test_login_normalizes_email_case():
    assert LoginRequest(email="USER@Example.com", password="x").email == "user@example.com"
