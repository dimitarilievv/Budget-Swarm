"""Tests for per-client rate limiting on /api/generate-trip (BS-093).

* **Unit** — the sliding-window counter allows N then blocks, and recovers once the
  window slides (a fake clock keeps it deterministic and instant).
* **Integration** — through the real app: a small limiter override makes the 3rd call
  return the ``429 RATE_LIMITED`` envelope, and a different client IP is unaffected.
"""

from __future__ import annotations

import pytest
from api.rate_limit import SlidingWindowRateLimiter, get_rate_limiter
from errors import RateLimitedError
from fastapi.testclient import TestClient
from main import app

VALID_REQUEST = {
    "budget": 1000,
    "duration_days": 5,
    "travelers": 2,
    "transport_mode": "car",
    "preferences": {"culture": 0.9, "food": 0.8},
    "origin_label": "Skopje",
    "vehicle_consumption": 6.5,
}


# --- unit -------------------------------------------------------------------


class _FakeClock:
    def __init__(self):
        self.t = 0.0

    def __call__(self):
        return self.t


def test_allows_up_to_the_limit_then_blocks():
    clock = _FakeClock()
    limiter = SlidingWindowRateLimiter(2, 60, clock=clock)
    limiter.check("ip")  # 1
    limiter.check("ip")  # 2
    with pytest.raises(RateLimitedError):
        limiter.check("ip")  # 3 -> over


def test_window_slides_and_recovers():
    clock = _FakeClock()
    limiter = SlidingWindowRateLimiter(2, 60, clock=clock)
    limiter.check("ip")
    limiter.check("ip")
    clock.t = 61  # the first two hits fall out of the window
    limiter.check("ip")  # allowed again


def test_separate_clients_have_separate_budgets():
    limiter = SlidingWindowRateLimiter(1, 60, clock=_FakeClock())
    limiter.check("a")
    limiter.check("b")  # different key, own budget
    with pytest.raises(RateLimitedError):
        limiter.check("a")


def test_non_positive_limit_disables_limiting():
    limiter = SlidingWindowRateLimiter(0, 60, clock=_FakeClock())
    for _ in range(100):
        limiter.check("ip")  # never raises


# --- integration ------------------------------------------------------------


@pytest.fixture
def client_with_small_limit():
    # One shared limiter instance across requests, so hits actually accumulate.
    limiter = SlidingWindowRateLimiter(2, 60)
    app.dependency_overrides[get_rate_limiter] = lambda: limiter
    try:
        yield TestClient(app)
    finally:
        app.dependency_overrides.pop(get_rate_limiter, None)


def test_generate_trip_returns_429_when_rate_limited(client_with_small_limit):
    client = client_with_small_limit
    assert client.post("/api/generate-trip", json=VALID_REQUEST).status_code == 200
    assert client.post("/api/generate-trip", json=VALID_REQUEST).status_code == 200

    resp = client.post("/api/generate-trip", json=VALID_REQUEST)
    assert resp.status_code == 429
    body = resp.json()
    assert body["error"]["code"] == "RATE_LIMITED"
    assert "rate limit" in body["error"]["message"].lower()


def test_rate_limit_is_per_client_ip(client_with_small_limit):
    client = client_with_small_limit
    # Exhaust one client's budget.
    hdr_a = {"X-Forwarded-For": "1.1.1.1"}
    client.post("/api/generate-trip", json=VALID_REQUEST, headers=hdr_a)
    client.post("/api/generate-trip", json=VALID_REQUEST, headers=hdr_a)
    assert client.post("/api/generate-trip", json=VALID_REQUEST, headers=hdr_a).status_code == 429
    # A different client IP still gets through.
    hdr_b = {"X-Forwarded-For": "2.2.2.2"}
    assert client.post("/api/generate-trip", json=VALID_REQUEST, headers=hdr_b).status_code == 200
