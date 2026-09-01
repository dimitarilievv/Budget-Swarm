"""Shared async HTTP session + per-provider rate-limit guards (BS-046).

Every external call BudgetSwarm makes goes through **one** :class:`httpx.AsyncClient`
so connection pooling, timeouts and header handling live in a single place, and
through a **per-provider token-bucket limiter** so the small free tiers this
project depends on (Travelpayouts, OpenRouteService, the Rome2Rio/Omio wrappers,
OpenTripMap) are never hammered past their quota — the same quota-protection goal
as the cache layer (technical-doc §23), applied at the request rather than the
result level.

Design:

* :class:`TokenBucket` — an async, monotonic-clock token bucket. ``capacity``
  permits a short burst; tokens refill at ``rate`` per second. :meth:`acquire`
  awaits until a permit is free. The clock and sleep are injectable so the
  limiter is unit-testable without wall-clock waits.
* :class:`ProviderConfig` — the per-provider registry entry: base URL, default
  headers, the rate/burst, and **how a server-side key is injected** (header,
  bearer, or query param). Keys are *passed in* by the backend from BS-015
  settings; this module never reads the environment and never hardcodes a key,
  so nothing here can leak to the frontend.
* :class:`HttpSession` — wraps the shared client and the registry. :meth:`request`
  looks up the provider, waits on its limiter, injects the key, and issues the
  call. Providers with no registry entry still work (no limiter, no key).

The session is created once server-side (see :func:`provider_registry_from_settings`
for the standard wiring) and shared by all providers; it is an async context
manager so the underlying client is closed cleanly on shutdown.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from enum import Enum
from types import TracebackType

import httpx

logger = logging.getLogger(__name__)

DEFAULT_TIMEOUT = 10.0

# Type of an async sleep, injectable for deterministic tests.
AsyncSleep = Callable[[float], Awaitable[None]]


# --- token-bucket rate limiter ----------------------------------------------


class TokenBucket:
    """An async token-bucket rate limiter (per provider).

    ``rate`` tokens are added per second up to ``capacity`` (the burst size,
    defaulting to one second's worth, min 1). :meth:`acquire` consumes a token,
    awaiting just long enough for one to refill when the bucket is empty — so a
    caller is *paced*, not rejected. ``monotonic`` and ``sleep`` are injectable so
    tests can drive a fake clock and assert the enforced delay without real waits.
    """

    def __init__(
        self,
        rate: float,
        capacity: float | None = None,
        *,
        monotonic: Callable[[], float] = time.monotonic,
        sleep: AsyncSleep = asyncio.sleep,
    ) -> None:
        if rate <= 0:
            raise ValueError("rate must be positive")
        self.rate = rate
        self.capacity = capacity if capacity is not None else max(1.0, rate)
        self._tokens = self.capacity
        self._monotonic = monotonic
        self._sleep = sleep
        self._updated = monotonic()
        self._lock = asyncio.Lock()

    def _refill(self, now: float) -> None:
        elapsed = max(0.0, now - self._updated)
        self._tokens = min(self.capacity, self._tokens + elapsed * self.rate)
        self._updated = now

    async def acquire(self, tokens: float = 1.0) -> float:
        """Consume ``tokens``, waiting if needed. Returns the seconds waited.

        The lock serialises waiters so the enforced spacing holds under
        concurrency (each waiter refills, then either consumes or sleeps for the
        exact deficit before retrying).
        """
        if tokens > self.capacity:
            raise ValueError(f"cannot acquire {tokens} tokens; capacity is {self.capacity}")
        waited = 0.0
        async with self._lock:
            while True:
                now = self._monotonic()
                self._refill(now)
                if self._tokens >= tokens:
                    self._tokens -= tokens
                    return waited
                deficit = tokens - self._tokens
                delay = deficit / self.rate
                logger.debug("rate limit: waiting %.3fs for a permit", delay)
                await self._sleep(delay)
                waited += delay


# --- provider registry ------------------------------------------------------


class KeyLocation(Enum):
    """Where a provider's server-side key goes on the outgoing request."""

    NONE = "none"
    HEADER = "header"  # a named header, e.g. X-Api-Key
    BEARER = "bearer"  # Authorization: Bearer <key>
    QUERY = "query"  # a query parameter, e.g. ?token=<key>


@dataclass
class ProviderConfig:
    """Per-provider HTTP settings: base URL, headers, rate limit, key injection.

    ``api_key`` is supplied by the backend from BS-015 settings (server-side); it
    is placed on each request according to ``key_location``/``key_name``. Leave
    ``api_key`` ``None`` for keyless providers (Open-Meteo, Overpass).
    """

    name: str
    base_url: str | None = None
    rate: float = 5.0  # requests per second
    capacity: float | None = None  # burst; defaults to one second's worth
    api_key: str | None = None
    key_location: KeyLocation = KeyLocation.NONE
    key_name: str | None = None  # header name (HEADER) or query param (QUERY)
    default_headers: Mapping[str, str] = field(default_factory=dict)

    def build_limiter(self, **clock: object) -> TokenBucket:
        return TokenBucket(self.rate, self.capacity, **clock)  # type: ignore[arg-type]

    def apply_auth(self, headers: dict[str, str], params: dict[str, object]) -> None:
        """Inject the server-side key into ``headers``/``params`` in place."""
        if not self.api_key or self.key_location is KeyLocation.NONE:
            return
        if self.key_location is KeyLocation.BEARER:
            headers["Authorization"] = f"Bearer {self.api_key}"
        elif self.key_location is KeyLocation.HEADER:
            headers[self.key_name or "X-Api-Key"] = self.api_key
        elif self.key_location is KeyLocation.QUERY:
            params[self.key_name or "key"] = self.api_key


# --- shared async session ----------------------------------------------------


class HttpSession:
    """A shared :class:`httpx.AsyncClient` plus per-provider rate limiters.

    Construct once server-side and share it across all providers. Use
    :meth:`request` (or :meth:`get`/:meth:`post`) with a ``provider`` name; the
    session waits on that provider's limiter and injects its key before issuing
    the call. Unknown providers pass through unthrottled and unauthenticated,
    which keeps keyless/ad-hoc calls simple.
    """

    def __init__(
        self,
        registry: Mapping[str, ProviderConfig] | None = None,
        *,
        client: httpx.AsyncClient | None = None,
        timeout: float = DEFAULT_TIMEOUT,
        monotonic: Callable[[], float] = time.monotonic,
        sleep: AsyncSleep = asyncio.sleep,
    ) -> None:
        self._registry = dict(registry or {})
        self._owns_client = client is None
        self._client = client or httpx.AsyncClient(timeout=timeout)
        self._limiters: dict[str, TokenBucket] = {
            name: cfg.build_limiter(monotonic=monotonic, sleep=sleep)
            for name, cfg in self._registry.items()
        }

    @property
    def client(self) -> httpx.AsyncClient:
        return self._client

    async def request(
        self,
        provider: str,
        method: str,
        url: str,
        *,
        headers: Mapping[str, str] | None = None,
        params: Mapping[str, object] | None = None,
        **kwargs: object,
    ) -> httpx.Response:
        """Issue ``method url`` for ``provider``, rate-limited and authenticated.

        Waits on the provider's limiter (if registered), merges the provider's
        default headers, injects its server-side key, and resolves ``url`` against
        the provider's ``base_url`` when ``url`` is relative.
        """
        cfg = self._registry.get(provider)
        merged_headers: dict[str, str] = dict(cfg.default_headers) if cfg else {}
        merged_headers.update(headers or {})
        merged_params: dict[str, object] = dict(params or {})

        if cfg is not None:
            cfg.apply_auth(merged_headers, merged_params)
            if cfg.base_url and not url.startswith(("http://", "https://")):
                url = cfg.base_url.rstrip("/") + "/" + url.lstrip("/")

        limiter = self._limiters.get(provider)
        if limiter is not None:
            waited = await limiter.acquire()
            if waited:
                logger.debug("rate limit: %s delayed %.3fs", provider, waited)

        return await self._client.request(
            method, url, headers=merged_headers, params=merged_params, **kwargs
        )

    async def get(self, provider: str, url: str, **kwargs: object) -> httpx.Response:
        return await self.request(provider, "GET", url, **kwargs)

    async def post(self, provider: str, url: str, **kwargs: object) -> httpx.Response:
        return await self.request(provider, "POST", url, **kwargs)

    async def aclose(self) -> None:
        """Close the underlying client if this session created it."""
        if self._owns_client:
            await self._client.aclose()

    async def __aenter__(self) -> HttpSession:
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        await self.aclose()


# --- standard server-side wiring --------------------------------------------


def provider_registry_from_settings(settings: object) -> dict[str, ProviderConfig]:
    """Build the standard provider registry from a BS-015 settings object.

    ``settings`` is duck-typed (any object exposing the ``*_api_key`` /
    ``*_token`` attributes of :class:`config.Settings`), so ``ai_engine`` never
    imports the backend. Keys are read here — server-side, from the environment —
    and never reach the frontend. Rates are deliberately conservative to protect
    the free tiers; keyless providers (Open-Meteo, Overpass) carry no key.
    """
    get = lambda attr: getattr(settings, attr, None)  # noqa: E731
    return {
        "open-meteo": ProviderConfig("open-meteo", rate=5.0),  # keyless
        "overpass": ProviderConfig("overpass", rate=1.0),  # keyless, be gentle
        "openrouteservice": ProviderConfig(
            "openrouteservice",
            rate=2.0,
            api_key=get("ors_api_key"),
            key_location=KeyLocation.HEADER,
            key_name="Authorization",
        ),
        "travelpayouts": ProviderConfig(
            "travelpayouts",
            rate=1.0,
            api_key=get("travelpayouts_token"),
            key_location=KeyLocation.HEADER,
            key_name="X-Access-Token",
        ),
        "public-transport": ProviderConfig(
            "public-transport",
            rate=1.0,
            api_key=get("public_transport_api_key"),
            key_location=KeyLocation.QUERY,
            key_name="key",
        ),
        "opentripmap": ProviderConfig(
            "opentripmap",
            rate=2.0,
            api_key=get("opentripmap_api_key"),
            key_location=KeyLocation.QUERY,
            key_name="apikey",
        ),
        "apify": ProviderConfig(
            "apify",
            rate=1.0,
            api_key=get("apify_token"),
            key_location=KeyLocation.QUERY,
            key_name="token",
        ),
    }
