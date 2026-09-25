"""CORS allow-list tests (BS-093, technical-doc §24).

The API must only grant CORS to the known frontend origin(s) — never a wildcard — so
a page on another origin cannot call it from a browser. We assert the
``Access-Control-Allow-Origin`` header is echoed for an allowed origin and withheld
for a disallowed one, on both a simple request and a preflight.
"""

from __future__ import annotations

from config import get_settings
from fastapi.testclient import TestClient
from main import app

client = TestClient(app)

ALLOWED = "http://localhost:5173"
DISALLOWED = "http://evil.example"


def test_settings_have_no_wildcard_origin():
    origins = get_settings().cors_origins_list
    assert origins and "*" not in origins


def test_allowed_origin_is_granted_cors():
    resp = client.get("/", headers={"Origin": ALLOWED})
    assert resp.headers.get("access-control-allow-origin") == ALLOWED


def test_disallowed_origin_is_not_granted_cors():
    resp = client.get("/", headers={"Origin": DISALLOWED})
    assert "access-control-allow-origin" not in resp.headers


def test_preflight_allowed_origin_is_granted():
    resp = client.options(
        "/api/generate-trip",
        headers={
            "Origin": ALLOWED,
            "Access-Control-Request-Method": "POST",
        },
    )
    assert resp.headers.get("access-control-allow-origin") == ALLOWED


def test_preflight_disallowed_origin_is_blocked():
    resp = client.options(
        "/api/generate-trip",
        headers={
            "Origin": DISALLOWED,
            "Access-Control-Request-Method": "POST",
        },
    )
    assert "access-control-allow-origin" not in resp.headers
