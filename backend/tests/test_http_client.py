"""Unit tests for the shared async HTTP session + rate-limit guards (BS-046).

No network and no event-loop plugin: async coroutines are driven with
``asyncio.run``, and outgoing requests are captured with ``httpx.MockTransport``.
Rate-limit timing is made deterministic with a fake clock whose ``sleep`` simply
advances virtual time, so "the limiter blocks over-rate calls" is asserted
exactly, with no real waiting.
"""

from __future__ import annotations

import asyncio

import httpx
import pytest

from ai_engine.utils.http_client import (
    HttpSession,
    KeyLocation,
    ProviderConfig,
    TokenBucket,
    provider_registry_from_settings,
)


class FakeClock:
    """Monotonic clock that only advances when ``sleep`` is awaited."""

    def __init__(self) -> None:
        self.t = 0.0

    def monotonic(self) -> float:
        return self.t

    async def sleep(self, delay: float) -> None:
        self.t += delay


def _mock_session(registry, capture, clock=None):
    """An HttpSession whose transport records the last outgoing request."""

    def handler(request: httpx.Request) -> httpx.Response:
        capture["request"] = request
        return httpx.Response(200, json={"ok": True})

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    kw = {"client": client}
    if clock is not None:
        kw |= {"monotonic": clock.monotonic, "sleep": clock.sleep}
    return HttpSession(registry, **kw)


# --- token bucket: over-rate calls are paced --------------------------------


def test_bucket_allows_burst_up_to_capacity_without_waiting():
    clock = FakeClock()
    bucket = TokenBucket(rate=2.0, capacity=2.0, monotonic=clock.monotonic, sleep=clock.sleep)
    assert asyncio.run(bucket.acquire()) == 0.0
    assert asyncio.run(bucket.acquire()) == 0.0  # still within the burst
    assert clock.t == 0.0


def test_bucket_blocks_over_rate_call_until_refill():
    clock = FakeClock()
    bucket = TokenBucket(rate=2.0, capacity=2.0, monotonic=clock.monotonic, sleep=clock.sleep)

    async def scenario():
        await bucket.acquire()
        await bucket.acquire()  # empties the burst
        return await bucket.acquire()  # must wait for one token to refill

    waited = asyncio.run(scenario())
    assert waited == pytest.approx(0.5)  # 1 token / 2 per sec
    assert clock.t == pytest.approx(0.5)


def test_bucket_rejects_request_larger_than_capacity():
    bucket = TokenBucket(rate=1.0, capacity=1.0)
    with pytest.raises(ValueError):
        asyncio.run(bucket.acquire(2.0))


def test_bucket_requires_positive_rate():
    with pytest.raises(ValueError):
        TokenBucket(rate=0)


# --- session rate limiting gates real requests ------------------------------


def test_session_paces_requests_over_provider_rate():
    clock = FakeClock()
    registry = {"slow": ProviderConfig("slow", rate=1.0, capacity=1.0)}
    capture: dict = {}
    session = _mock_session(registry, capture, clock=clock)

    async def scenario():
        await session.get("slow", "https://x.test/a")  # consumes the one token
        await session.get("slow", "https://x.test/b")  # must wait ~1s
        await session.aclose()

    asyncio.run(scenario())
    assert clock.t == pytest.approx(1.0)


def test_unknown_provider_passes_through_unthrottled():
    clock = FakeClock()
    capture: dict = {}
    session = _mock_session({}, capture, clock=clock)

    async def scenario():
        resp = await session.get("nobody", "https://x.test/ping")
        await session.aclose()
        return resp

    resp = asyncio.run(scenario())
    assert resp.status_code == 200 and clock.t == 0.0


# --- server-side key injection ----------------------------------------------


def test_header_key_injected_server_side():
    registry = {
        "tp": ProviderConfig(
            "tp",
            base_url="https://api.tp.test",
            rate=100.0,
            api_key="secret-token",
            key_location=KeyLocation.HEADER,
            key_name="X-Access-Token",
        )
    }
    capture: dict = {}
    session = _mock_session(registry, capture)

    async def scenario():
        await session.get("tp", "/prices", params={"origin": "SKP"})
        await session.aclose()

    asyncio.run(scenario())
    req = capture["request"]
    assert req.headers["X-Access-Token"] == "secret-token"
    assert str(req.url) == "https://api.tp.test/prices?origin=SKP"  # base_url + relative path


def test_bearer_key_injected():
    registry = {
        "b": ProviderConfig("b", api_key="abc", key_location=KeyLocation.BEARER, rate=100.0)
    }
    capture: dict = {}
    session = _mock_session(registry, capture)
    asyncio.run(_run_and_close(session, "b", "https://x.test/y"))
    assert capture["request"].headers["Authorization"] == "Bearer abc"


def test_query_key_injected():
    registry = {
        "q": ProviderConfig(
            "q", api_key="k123", key_location=KeyLocation.QUERY, key_name="apikey", rate=100.0
        )
    }
    capture: dict = {}
    session = _mock_session(registry, capture)
    asyncio.run(_run_and_close(session, "q", "https://x.test/y"))
    assert capture["request"].url.params.get("apikey") == "k123"


def test_keyless_provider_sends_no_auth():
    registry = {"open-meteo": ProviderConfig("open-meteo", rate=100.0)}
    capture: dict = {}
    session = _mock_session(registry, capture)
    asyncio.run(_run_and_close(session, "open-meteo", "https://x.test/forecast"))
    req = capture["request"]
    assert "Authorization" not in req.headers and "X-Api-Key" not in req.headers


async def _run_and_close(session: HttpSession, provider: str, url: str):
    await session.get(provider, url)
    await session.aclose()


# --- standard wiring from settings ------------------------------------------


class _Settings:
    """Duck-typed stand-in for config.Settings (BS-015)."""

    ors_api_key = "ors-key"
    travelpayouts_token = "tp-token"
    public_transport_api_key = None
    opentripmap_api_key = "otm-key"
    apify_token = None


def test_registry_from_settings_reads_keys_server_side():
    reg = provider_registry_from_settings(_Settings())
    assert reg["openrouteservice"].api_key == "ors-key"
    assert reg["travelpayouts"].api_key == "tp-token"
    assert reg["opentripmap"].api_key == "otm-key"
    # Keyless / unset providers carry no key.
    assert reg["open-meteo"].api_key is None
    assert reg["public-transport"].api_key is None
    assert reg["apify"].api_key is None
