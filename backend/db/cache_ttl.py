"""Per-data-type TTL policy for the ``api_cache`` read-through (Phase 10).

The read-through in :mod:`db.retrieval` applies whatever TTL it is handed; this
module is the single place that decides that TTL *by kind of data*, per
technical-doc §23, so every provider caches for a duration appropriate to how
fast its data changes:

===============  ==========================================  ==================
Data type        Why                                         Default freshness
===============  ==========================================  ==================
``weather``      forecasts move within hours                 6 hours (short)
``price``        indicative flight/train prices              12 hours (short-med)
``route``        origin→destination distances are stable     7 days (medium)
``poi``          attractions change over weeks               30 days (long)
``reference``    curated destinations / features             long-lived (none)
``accommodation``snapshot data, refreshed by re-ingest       long-lived (none)
===============  ==========================================  ==================

A TTL of ``None`` means "never expires" (long-lived reference data). Defaults can
be overridden per deployment with ``CACHE_TTL_<TYPE>_HOURS`` (e.g.
``CACHE_TTL_WEATHER_HOURS=3``); a value of ``0`` or less means long-lived.

Kept free of DB and settings imports so it is importable anywhere — including the
``ai_engine`` providers — without a configured database.
"""

from __future__ import annotations

import logging
import os
from datetime import timedelta
from enum import StrEnum

logger = logging.getLogger(__name__)


class CacheType(StrEnum):
    """The kinds of cached data, each with its own freshness policy (§23)."""

    WEATHER = "weather"
    PRICE = "price"
    ROUTE = "route"
    POI = "poi"
    REFERENCE = "reference"
    ACCOMMODATION = "accommodation"


# Sensible defaults per §23. ``None`` == long-lived (no expiry).
DEFAULT_TTLS: dict[CacheType, timedelta | None] = {
    CacheType.WEATHER: timedelta(hours=6),
    CacheType.PRICE: timedelta(hours=12),
    CacheType.ROUTE: timedelta(days=7),
    CacheType.POI: timedelta(days=30),
    CacheType.REFERENCE: None,
    CacheType.ACCOMMODATION: None,
}

# Sentinel so "no override set" is distinct from an override of "long-lived".
_MISSING: object = object()


def _coerce(cache_type: CacheType | str | None) -> CacheType | None:
    """Accept a CacheType, its string value, or None; return the enum or None."""
    if isinstance(cache_type, CacheType):
        return cache_type
    if isinstance(cache_type, str):
        try:
            return CacheType(cache_type.lower())
        except ValueError:
            return None
    return None


def _env_override(ct: CacheType) -> timedelta | None | object:
    """Read ``CACHE_TTL_<TYPE>_HOURS``; ``_MISSING`` when unset, ``None`` for ≤ 0."""
    raw = os.environ.get(f"CACHE_TTL_{ct.name}_HOURS")
    if raw is None or not raw.strip():
        return _MISSING
    try:
        hours = float(raw)
    except ValueError:
        logger.warning("cache_ttl: invalid CACHE_TTL_%s_HOURS=%r; using default", ct.name, raw)
        return _MISSING
    return timedelta(hours=hours) if hours > 0 else None


def ttl_for(cache_type: CacheType | str | None) -> timedelta | None:
    """Return the freshness TTL for a data type (``None`` = long-lived / never expires).

    An unknown or missing type is treated as long-lived so a mis-tagged caller
    caches conservatively rather than crashing.
    """
    ct = _coerce(cache_type)
    if ct is None:
        if cache_type is not None:
            logger.warning("cache_ttl: unknown cache type %r; treating as long-lived", cache_type)
        return None
    override = _env_override(ct)
    if override is not _MISSING:
        return override  # type: ignore[return-value]
    return DEFAULT_TTLS[ct]
