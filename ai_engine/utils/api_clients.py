"""Provider abstraction layer: the common contract every external source sits
behind, plus the cross-cutting behaviour it reuses (BS-045; technical-doc §21/§22).

**Goal — no vendor lock-in.** Agents depend on *capabilities*, not *vendors*: a
``FlightProvider``/``WeatherProvider``/… exposes one normalized :meth:`~BaseProvider.fetch`
that returns an internal shape, so the concrete source can change without touching
agent logic. This is what let the Amadeus shutdown be absorbed without an
architectural change — its slot was always an interface, not a hard-coded call.

The interface is also the single, natural home for the behaviour every provider
needs and none should re-implement (technical-doc §21 "resilience"):

* **timeout** — each provider carries a request ``timeout`` its HTTP client
  enforces; a timeout is classified as transient and retried.
* **retry with backoff** — transient failures (timeout / 5xx / network) are
  retried with exponential backoff before the source is declared down
  (:class:`RetryPolicy`).
* **cache hook** — a :class:`CacheHook` reads the DB/cache first and only spends
  an external call on a miss, tagging the result's provenance. The production
  hook wraps BS-037's ``read_through`` (see :func:`read_through_hook`); the base
  never imports the backend, so ``ai_engine`` stays independent of it.
* **quality_flag tagging** — every result is stamped ``real`` / ``cached`` /
  ``estimated`` (the schema's three provenance values) so the optimizer and the
  UI stay honest about how firm a number is (technical-doc §22 "degrade, don't
  crash").
* **fallback chaining** — :func:`fetch_with_fallback` composes providers
  primary→fallback→…→estimate and returns the first usable answer, flagging a
  computed estimate as ``estimated``.

Concrete providers (BS-046..053, BS-090/092) subclass :class:`BaseProvider` and
implement only :meth:`~BaseProvider._fetch` — the vendor call plus the mapping of
its response into the internal schema. Everything else is inherited.
"""

from __future__ import annotations

import logging
import random
import time
from abc import ABC, abstractmethod
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import timedelta
from typing import Generic, Protocol, TypeVar, runtime_checkable

logger = logging.getLogger(__name__)

# quality_flag values, mirroring the schema CHECK ('real','cached','estimated')
# and db.retrieval's constants. Duplicated (not imported) so ai_engine carries no
# dependency on the backend package.
QUALITY_REAL = "real"
QUALITY_CACHED = "cached"
QUALITY_ESTIMATED = "estimated"

Req = TypeVar("Req")
Data = TypeVar("Data")


# --- errors -----------------------------------------------------------------


class ProviderError(Exception):
    """Base class for every provider failure the fallback chain understands."""


class ProviderUnavailable(ProviderError):
    """The source could not be reached or failed transiently.

    Timeouts, connection errors and 5xx responses. **Retryable**, and — once
    retries are exhausted — a trigger to fall through to the next provider.
    """


class ProviderNoResult(ProviderError):
    """The source was reached but has no data for this request.

    An empty result or an uncovered destination. **Not** retryable (retrying the
    same call yields the same emptiness), but still a trigger to try the next
    provider or the estimate.
    """


class AllProvidersFailed(ProviderError):
    """Every provider in a chain failed and no estimate was supplied."""

    def __init__(self, attempts: Sequence[tuple[str, object]]) -> None:
        self.attempts = list(attempts)
        detail = ", ".join(f"{name}: {reason}" for name, reason in self.attempts) or "none tried"
        super().__init__(f"all providers failed ({detail})")


# --- result + cache-hook contracts ------------------------------------------


@dataclass
class ProviderResult(Generic[Data]):
    """A provider's answer plus where it came from.

    ``quality_flag`` is one of :data:`QUALITY_REAL` / :data:`QUALITY_CACHED` /
    :data:`QUALITY_ESTIMATED`; ``source`` is a coarse provenance label
    (``live`` / ``cache`` / ``estimate``); ``stale`` is true only when a stale
    cache value was served because a live fetch failed (degrade-don't-crash).
    """

    data: Data
    quality_flag: str
    provider: str
    source: str
    stale: bool = False


@runtime_checkable
class CacheOutcome(Protocol):
    """Structural shape the cache hook returns — satisfied by db.retrieval.Retrieved.

    Kept structural (not an import) so the base is testable with fakes and free of
    a backend dependency.
    """

    data: object
    quality_flag: str
    source: str
    stale: bool


class CacheHook(Protocol):
    """A read-through cache: serve a fresh entry, else call ``fetch_fn`` and store.

    The signature matches a store-bound :func:`db.retrieval.read_through`; use
    :func:`read_through_hook` to build one from it. A hook must call ``fetch_fn``
    only on a miss/stale entry (quota protection) and tag provenance in the
    :class:`CacheOutcome` it returns.
    """

    def __call__(
        self,
        provider: str,
        params: object,
        fetch_fn: Callable[[], object],
        *,
        ttl: timedelta | None,
    ) -> CacheOutcome: ...


def read_through_hook(read_through: Callable[..., CacheOutcome], store: object) -> CacheHook:
    """Adapt BS-037's ``read_through`` + a cache store into a :class:`CacheHook`.

    Dependency-injected on purpose: the caller (the backend) imports
    ``db.retrieval.read_through`` and its ``ApiCacheStore`` and passes them in, so
    ``ai_engine`` never imports the backend itself::

        from db.retrieval import read_through, ApiCacheStore
        hook = read_through_hook(read_through, ApiCacheStore(session))
        provider = OpenMeteoProvider(cache=hook)
    """

    def hook(
        provider: str,
        params: object,
        fetch_fn: Callable[[], object],
        *,
        ttl: timedelta | None,
    ) -> CacheOutcome:
        return read_through(store, provider, params, fetch_fn, ttl=ttl)

    return hook


# --- retry / backoff --------------------------------------------------------


@dataclass(frozen=True)
class RetryPolicy:
    """Exponential backoff for transient provider failures.

    ``attempts`` total tries; between them the delay is
    ``min(max_backoff, backoff * 2**(n-1))`` seconds, multiplied by ``1 + jitter``
    to avoid synchronised retries against a shared upstream. ``sleep`` and
    ``jitter`` are injectable so tests run instantly and deterministically. Only
    ``retry_on`` exceptions are retried; anything else propagates immediately.
    """

    attempts: int = 3
    backoff: float = 0.5
    max_backoff: float = 8.0
    retry_on: tuple[type[BaseException], ...] = (ProviderUnavailable, TimeoutError)
    sleep: Callable[[float], None] = time.sleep
    jitter: Callable[[], float] = lambda: random.random() * 0.1  # noqa: E731

    def run(self, call: Callable[[], Data], *, what: str = "provider") -> Data:
        """Call ``call``, retrying transient failures; re-raise the last on give-up."""
        last: BaseException | None = None
        for attempt in range(1, self.attempts + 1):
            try:
                return call()
            except self.retry_on as exc:
                last = exc
                if attempt >= self.attempts:
                    break
                delay = min(self.max_backoff, self.backoff * 2 ** (attempt - 1))
                delay *= 1 + self.jitter()
                logger.warning(
                    "%s: attempt %d/%d failed (%s); retrying in %.2fs",
                    what,
                    attempt,
                    self.attempts,
                    exc,
                    delay,
                )
                self.sleep(delay)
        assert last is not None  # loop only breaks after catching
        raise last


# --- base provider ----------------------------------------------------------


class BaseProvider(ABC, Generic[Req, Data]):
    """Common base for every external-source adapter.

    A subclass sets :attr:`name` and implements :meth:`_fetch` — the vendor call
    that returns the *already normalized* internal shape (or raises
    :class:`ProviderUnavailable` / :class:`ProviderNoResult`). The base supplies
    the cross-cutting behaviour: :meth:`fetch` runs the retry policy, threads the
    call through the cache hook when one is configured, and stamps the
    ``quality_flag`` / provenance onto a :class:`ProviderResult`.

    Timeout is delegated to the concrete HTTP client via :attr:`timeout` (the
    honest place to enforce it); the base classifies a resulting
    :class:`ProviderUnavailable`/``TimeoutError`` as transient and retries it.
    """

    #: short, stable identifier — also the cache ``provider`` key.
    name: str = "provider"

    def __init__(
        self,
        *,
        name: str | None = None,
        cache: CacheHook | None = None,
        ttl: timedelta | None = None,
        timeout: float | None = 10.0,
        retry: RetryPolicy | None = None,
    ) -> None:
        if name is not None:
            self.name = name
        self.cache = cache
        self.ttl = ttl
        self.timeout = timeout
        self.retry = retry or RetryPolicy()

    @abstractmethod
    def _fetch(self, request: Req) -> Data:
        """Perform the vendor call and return the normalized internal shape.

        Implementations should enforce :attr:`timeout` on their HTTP client, raise
        :class:`ProviderUnavailable` for transient errors (timeout/5xx/network)
        and :class:`ProviderNoResult` when the source has no data for ``request``.
        """

    def cache_key(self, request: Req) -> object:
        """The cache signature for ``request``. Override when the raw request is
        not a stable/serialisable key; the default uses it as-is (db.retrieval's
        ``make_signature`` canonicalises dicts and hashes overlong keys)."""
        return request

    def fetch(self, request: Req) -> ProviderResult[Data]:
        """Return normalized data for ``request``, cache- and retry-aware.

        With no cache hook the retried live call is returned as ``real``. With a
        hook, the call is threaded through it: a fresh entry comes back ``cached``
        without touching the network, a miss triggers the retried fetch and is
        stored + returned ``real``, and a fetch failure over a stale entry degrades
        to that stale value (``cached`` + ``stale``) instead of crashing.
        """

        def live() -> Data:
            return self.retry.run(lambda: self._fetch(request), what=self.name)

        if self.cache is None:
            return ProviderResult(live(), QUALITY_REAL, self.name, "live")

        outcome = self.cache(self.name, self.cache_key(request), live, ttl=self.ttl)
        return ProviderResult(
            outcome.data,
            outcome.quality_flag,
            self.name,
            outcome.source,
            stale=getattr(outcome, "stale", False),
        )


class EstimateProvider(BaseProvider[Req, Data], ABC):
    """A deterministic, always-available fallback (e.g. distance-based fare).

    It never fails and never hits the network, so it is not cached and its output
    is always flagged :data:`QUALITY_ESTIMATED`. Subclasses implement
    :meth:`_estimate`; place one last in a :func:`fetch_with_fallback` chain (or
    pass its :meth:`_estimate` as the helper's ``estimate=``) to guarantee the
    chain always yields a usable, honestly-flagged value.
    """

    name = "estimate"

    def __init__(self, *, name: str | None = None) -> None:
        super().__init__(name=name, cache=None)

    @abstractmethod
    def _estimate(self, request: Req) -> Data:
        """Compute the deterministic estimate for ``request``."""

    def _fetch(self, request: Req) -> Data:  # pragma: no cover - trivial delegation
        return self._estimate(request)

    def fetch(self, request: Req) -> ProviderResult[Data]:
        return ProviderResult(self._estimate(request), QUALITY_ESTIMATED, self.name, "estimate")


# --- fallback chain ---------------------------------------------------------


def _is_empty(data: object) -> bool:
    """Treat ``None`` and empty containers/strings as "no result"; keep scalars."""
    if data is None:
        return True
    if isinstance(data, (str, bytes, list, tuple, dict, set)):
        return len(data) == 0
    return False


def fetch_with_fallback(
    providers: Sequence[BaseProvider[Req, Data]],
    request: Req,
    *,
    estimate: Callable[[Req], Data] | None = None,
) -> ProviderResult[Data]:
    """Try each provider in order; return the first usable result, else estimate.

    Implements the technical-doc §22 chain *primary → fallback → … → estimate*.
    A provider is skipped when it raises :class:`ProviderError` (unavailable after
    its own retries, or no result) or returns empty data; the next is tried. If
    all are exhausted and ``estimate`` is given, its value is returned flagged
    :data:`QUALITY_ESTIMATED`; otherwise :class:`AllProvidersFailed` is raised.

    Note each provider has already applied its own retry/backoff and cache in
    :meth:`BaseProvider.fetch`, so this helper only sequences *across* sources.
    """
    attempts: list[tuple[str, object]] = []
    for provider in providers:
        try:
            result = provider.fetch(request)
        except ProviderError as exc:
            logger.info("fallback: %s failed (%s); trying next", provider.name, exc)
            attempts.append((provider.name, exc))
            continue
        if _is_empty(result.data):
            logger.info("fallback: %s returned empty; trying next", provider.name)
            attempts.append((provider.name, "empty"))
            continue
        return result

    if estimate is not None:
        logger.info("fallback: all providers exhausted; using estimate")
        return ProviderResult(estimate(request), QUALITY_ESTIMATED, "estimate", "estimate")

    raise AllProvidersFailed(attempts)
