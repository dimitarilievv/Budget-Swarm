"""DB/cache-first retrieval helpers (BS-037).

Agents and providers should read what BudgetSwarm already has — the reference
tables and the ``api_cache`` — before spending an external API call (ADR-008;
quota protection + speed). This module is the read-through those callers use:

    result = read_through(store, "open-meteo", params, fetch_fn=call_open_meteo,
                          ttl=timedelta(hours=6))
    # result.data is the payload; result.quality_flag is 'cached' on a hit,
    # 'real' on a fresh fetch. The app stays honest about provenance.

A **fresh** cache entry is returned immediately and marked ``cached``; a missing
or **stale** entry falls through to ``fetch_fn``, whose result is stored (with a
TTL) and returned marked ``real``. If the fetch fails but a stale entry exists,
that stale value is served (marked ``cached``, ``stale=True``) rather than letting
the whole request fail — the degrade-don't-crash rule (ADR-005). ``fetch_fn`` is
never called when a fresh entry is present, so quotas are protected.

The cache is reached through a tiny :class:`CacheStore` protocol so the hit / miss
/ stale logic is pure and unit-testable without a database; :class:`ApiCacheStore`
is the production implementation over the ``api_cache`` table.
"""

from __future__ import annotations

import hashlib
import json
import logging
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Protocol, runtime_checkable

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from db.models import ApiCache

logger = logging.getLogger(__name__)

# quality_flag values (schema CHECK: 'real','cached','estimated').
QUALITY_REAL = "real"
QUALITY_CACHED = "cached"

# api_cache.query_signature is VARCHAR(400); longer canonical keys are hashed.
_MAX_SIGNATURE_LEN = 400

JsonBody = dict | list | str | int | float | bool | None


def make_signature(params: object) -> str:
    """Build a stable ``query_signature`` for a set of request params.

    A string is used as-is; anything else becomes canonical JSON (sorted keys), so
    the same logical request always yields the same signature regardless of dict
    ordering. Keys longer than the column are SHA-256 hashed to stay within it.
    """
    canonical = (
        params
        if isinstance(params, str)
        else json.dumps(params, sort_keys=True, separators=(",", ":"), default=str)
    )
    if len(canonical) <= _MAX_SIGNATURE_LEN:
        return canonical
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


@dataclass
class CacheEntry:
    """One stored cache row, source-agnostic."""

    body: JsonBody
    fetched_at: datetime
    expires_at: datetime | None

    def is_fresh(self, now: datetime) -> bool:
        """Fresh when there is no expiry (long-lived) or it is still in the future."""
        return self.expires_at is None or self.expires_at > now


@dataclass
class Retrieved:
    """The result of a read-through: the data plus where it came from."""

    data: JsonBody
    quality_flag: str  # 'cached' on a cache hit, 'real' on a fresh fetch
    source: str  # 'cache' | 'live'
    fetched_at: datetime | None
    expires_at: datetime | None = None
    stale: bool = False  # true only when a stale entry was served after a fetch error


@runtime_checkable
class CacheStore(Protocol):
    """Minimal cache contract the read-through needs (keeps it DB-agnostic)."""

    def get(self, provider: str, signature: str) -> CacheEntry | None: ...

    def put(
        self, provider: str, signature: str, body: JsonBody, expires_at: datetime | None
    ) -> None: ...


class ApiCacheStore:
    """:class:`CacheStore` over the ``api_cache`` table (provider, query_signature)."""

    def __init__(self, session: Session) -> None:
        self.session = session

    def get(self, provider: str, signature: str) -> CacheEntry | None:
        # api_cache PK is a surrogate id; the conceptual key is the unique
        # (provider, query_signature) pair.
        row = self.session.scalars(
            select(ApiCache).where(
                ApiCache.provider == provider,
                ApiCache.query_signature == signature,
            )
        ).first()
        if row is None:
            return None
        return CacheEntry(row.response_body, row.fetched_at, row.expires_at)

    def put(
        self, provider: str, signature: str, body: JsonBody, expires_at: datetime | None
    ) -> None:
        stmt = pg_insert(ApiCache).values(
            provider=provider,
            query_signature=signature,
            response_body=body,
            fetched_at=datetime.now(UTC),
            expires_at=expires_at,
        )
        stmt = stmt.on_conflict_do_update(
            index_elements=["provider", "query_signature"],
            set_={
                "response_body": stmt.excluded.response_body,
                "fetched_at": stmt.excluded.fetched_at,
                "expires_at": stmt.excluded.expires_at,
            },
        )
        self.session.execute(stmt)


def get_cached(store: CacheStore, provider: str, params: object) -> Retrieved | None:
    """Pure cache read: return a fresh entry marked ``cached``, else ``None``.

    Never fetches. For callers that want to consult the cache but handle a miss
    themselves.
    """
    signature = make_signature(params)
    entry = store.get(provider, signature)
    if entry is None or not entry.is_fresh(datetime.now(UTC)):
        return None
    return Retrieved(entry.body, QUALITY_CACHED, "cache", entry.fetched_at, entry.expires_at)


def read_through(
    store: CacheStore,
    provider: str,
    params: object,
    fetch_fn: Callable[[], JsonBody],
    *,
    ttl: timedelta | None = None,
    allow_stale_on_error: bool = True,
    now_fn: Callable[[], datetime] = lambda: datetime.now(UTC),
) -> Retrieved:
    """Return cached data when fresh, else fetch, store, and return it.

    * **Hit** — a fresh entry is returned marked ``cached``; ``fetch_fn`` is not
      called (quota protected).
    * **Miss / stale** — ``fetch_fn`` is called, its result stored with ``ttl``
      (``None`` = long-lived), and returned marked ``real``.
    * **Fetch error with a stale entry** — the stale value is served marked
      ``cached`` + ``stale`` (unless ``allow_stale_on_error`` is False); with no
      entry to fall back on the error propagates.
    """
    signature = make_signature(params)
    now = now_fn()
    entry = store.get(provider, signature)

    if entry is not None and entry.is_fresh(now):
        logger.debug("retrieval: %s cache hit", provider)
        return Retrieved(entry.body, QUALITY_CACHED, "cache", entry.fetched_at, entry.expires_at)

    try:
        data = fetch_fn()
    except Exception as exc:  # noqa: BLE001 - degrade to stale cache if we can (ADR-005)
        if allow_stale_on_error and entry is not None:
            logger.warning("retrieval: %s fetch failed (%s); serving stale cache", provider, exc)
            return Retrieved(
                entry.body, QUALITY_CACHED, "cache", entry.fetched_at, entry.expires_at, stale=True
            )
        raise

    expires_at = now + ttl if ttl is not None else None
    store.put(provider, signature, data, expires_at)
    logger.debug("retrieval: %s fetched live and cached (ttl=%s)", provider, ttl)
    return Retrieved(data, QUALITY_REAL, "live", now, expires_at)
