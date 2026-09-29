"""Per-client rate limiting for POST /api/generate-trip (BS-093, technical-doc §24).

A single client must not be able to exhaust the backend's own small upstream free
tiers (Open-Meteo, ORS, Travelpayouts, …). This applies a **sliding-window** limit
per client IP: at most ``rate_limit_requests`` calls per ``rate_limit_window_seconds``.
Exceeding it raises :class:`~errors.RateLimitedError`, which the global handler renders
as the ``429 RATE_LIMITED`` envelope (api.md).

In-memory and dependency-free — right-sized for a single-process demo (ADR: don't
over-engineer). It is wired as a FastAPI dependency so tests can override it, and the
window/limit are configurable via ``RATE_LIMIT_*`` env vars.
"""

from __future__ import annotations

import logging
import threading
import time
from collections import defaultdict, deque
from functools import lru_cache

from config import get_settings
from errors import RateLimitedError
from fastapi import Depends, Request

logger = logging.getLogger(__name__)


class SlidingWindowRateLimiter:
    """Thread-safe sliding-window counter keyed by an opaque client id.

    Keeps the timestamps of each key's recent hits and, on every check, drops those
    older than the window before counting — so the limit is enforced over any rolling
    ``window`` seconds rather than resetting on fixed boundaries.
    """

    def __init__(
        self,
        max_requests: int,
        window_seconds: float,
        *,
        clock=time.monotonic,
    ) -> None:
        self.max_requests = max_requests
        self.window_seconds = window_seconds
        self._clock = clock
        self._hits: dict[str, deque[float]] = defaultdict(deque)
        self._lock = threading.Lock()

    def check(self, key: str) -> None:
        """Record a hit for ``key``; raise :class:`RateLimitedError` if over the limit."""
        if self.max_requests <= 0:  # a non-positive limit disables limiting
            return
        now = self._clock()
        cutoff = now - self.window_seconds
        with self._lock:
            hits = self._hits[key]
            while hits and hits[0] <= cutoff:
                hits.popleft()
            if len(hits) >= self.max_requests:
                retry_after = max(1, int(self.window_seconds - (now - hits[0])) + 1)
                logger.info("rate limit hit for client %s (retry in ~%ds)", key, retry_after)
                raise RateLimitedError(
                    f"Rate limit exceeded: at most {self.max_requests} requests per "
                    f"{int(self.window_seconds)}s. Try again in ~{retry_after}s."
                )
            hits.append(now)
            if not hits:  # never true here, but keeps the map from growing unbounded
                del self._hits[key]


def client_key(request: Request) -> str:
    """Identify the client for rate limiting.

    Honours ``X-Forwarded-For`` (first hop) when present — the app runs behind a
    reverse proxy / Docker — and falls back to the direct peer address.
    """
    forwarded = request.headers.get("x-forwarded-for")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return request.client.host if request.client else "unknown"


@lru_cache
def get_rate_limiter() -> SlidingWindowRateLimiter:
    """The process-wide limiter, built from settings (cached). Overridable in tests."""
    settings = get_settings()
    return SlidingWindowRateLimiter(
        settings.rate_limit_requests, settings.rate_limit_window_seconds
    )


def enforce_rate_limit(
    request: Request,
    limiter: SlidingWindowRateLimiter = Depends(get_rate_limiter),
) -> None:
    """FastAPI dependency: enforce the per-client limit unless disabled in settings.

    The limiter is injected via :func:`get_rate_limiter` so tests can override it with a
    small window through ``app.dependency_overrides``.
    """
    if not get_settings().rate_limit_enabled:
        return
    limiter.check(client_key(request))
