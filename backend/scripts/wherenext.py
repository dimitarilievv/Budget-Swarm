"""WhereNext cost-of-living provider (BS-011 / BS-031).

Resolves per-country cost-of-living indices from **WhereNext**
(getwherenext.com) — the primary cost-of-living source decided in BS-011: free,
no-auth, CC BY 4.0 open data built on institutional sources (World Bank ICP,
OECD, Eurostat). The live endpoint returns 95 countries, each with a numeric
``cost_index`` (US baseline = 82, higher = more expensive) plus sub-indices.

Following the documented fallback chain (ADR-005) and the "cache into Postgres
rather than call live" principle (ADR-008), values are resolved in this order:

* ``cache``    — a fresh ``api_cache`` row (provider ``wherenext``), populated by
  ``scripts.ingest_cost_of_living``; avoids re-hitting the API (100 req/hour).
* ``live``     — an HTTPS GET to the WhereNext API; the raw payload is written
  back to ``api_cache`` when a session is available.
* ``snapshot`` — the committed ``data/wherenext_cost_of_living.json`` snapshot,
  the always-available offline/CI floor so the pipeline is reproducible without
  network (the same snapshot ships the WhereNext ``meta`` block for attribution).

``source="auto"`` walks that chain; the other values force a single source and
are used by the ingestion CLI (``live``) and by tests/CI (``snapshot``).
"""

from __future__ import annotations

import json
import logging
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path

from db.models import ApiCache
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from scripts.cost_of_living import ProviderResult

logger = logging.getLogger(__name__)

# backend/scripts/wherenext.py -> repo root is two parents up.
REPO_ROOT = Path(__file__).resolve().parents[2]
SNAPSHOT_PATH = REPO_ROOT / "data" / "wherenext_cost_of_living.json"

WHERENEXT_URL = "https://getwherenext.com/api/data/cost-of-living"
CACHE_PROVIDER = "wherenext"
CACHE_SIGNATURE = "cost-of-living"
DEFAULT_TTL = timedelta(days=30)  # institutional data changes slowly

VALID_SOURCES = ("auto", "live", "cache", "snapshot")


@dataclass
class CostIndexResult:
    """Resolved per-country cost indices and where they came from."""

    mapping: dict[str, float]  # ISO alpha-2 country code -> cost_index
    source: str  # one of live | cache | snapshot
    meta: dict[str, object] = field(default_factory=dict)

    @property
    def count(self) -> int:
        return len(self.mapping)


def parse_payload(payload: object) -> dict[str, float]:
    """Extract ``country_code -> cost_index`` from a WhereNext payload.

    Malformed records (missing/!=2-char code, non-numeric or negative index) are
    logged and skipped. Raises ``ValueError`` if the payload shape is wrong or no
    usable record remains.
    """
    if not isinstance(payload, dict) or not isinstance(payload.get("data"), list):
        raise ValueError("WhereNext payload is not an object with a 'data' array")

    mapping: dict[str, float] = {}
    for record in payload["data"]:
        if not isinstance(record, dict):
            continue
        code = str(record.get("country_code", "")).strip().upper()
        raw_index = record.get("cost_index")
        if len(code) != 2 or not code.isalpha():
            logger.warning(
                "WhereNext: skipping record with bad country_code %r", record.get("country_code")
            )
            continue
        try:
            index = float(raw_index)
        except (TypeError, ValueError):
            logger.warning("WhereNext: skipping %s, non-numeric cost_index %r", code, raw_index)
            continue
        if index < 0:
            logger.warning("WhereNext: skipping %s, negative cost_index %r", code, index)
            continue
        mapping[code] = index

    if not mapping:
        raise ValueError("WhereNext payload contained no usable cost_index records")
    return mapping


def fetch_live(timeout: float = 15.0) -> dict[str, object]:
    """GET the live WhereNext cost-of-living payload (raises on network error)."""
    logger.info("WhereNext: fetching live from %s", WHERENEXT_URL)
    request = urllib.request.Request(WHERENEXT_URL, headers={"Accept": "application/json"})
    with urllib.request.urlopen(request, timeout=timeout) as response:  # noqa: S310 (https only)
        payload = json.load(response)
    if not isinstance(payload, dict):
        raise ValueError("WhereNext live response was not a JSON object")
    return payload


def load_snapshot(path: Path = SNAPSHOT_PATH) -> dict[str, object]:
    """Load the committed WhereNext snapshot payload."""
    if not path.is_file():
        raise FileNotFoundError(f"WhereNext snapshot not found: {path}")
    with path.open(encoding="utf-8") as fh:
        payload = json.load(fh)
    if not isinstance(payload, dict):
        raise ValueError(f"WhereNext snapshot {path.name} is not a JSON object")
    return payload


def _cache_get_fresh(session: Session) -> dict[str, object] | None:
    now = datetime.now(UTC)
    stmt = select(ApiCache).where(
        ApiCache.provider == CACHE_PROVIDER,
        ApiCache.query_signature == CACHE_SIGNATURE,
        (ApiCache.expires_at.is_(None)) | (ApiCache.expires_at > now),
    )
    row = session.scalars(stmt).first()
    return row.response_body if row is not None else None


def _cache_put(session: Session, payload: dict[str, object], ttl: timedelta) -> None:
    """Upsert the raw WhereNext payload into ``api_cache`` (provider ``wherenext``)."""
    expires_at = datetime.now(UTC) + ttl
    stmt = pg_insert(ApiCache).values(
        provider=CACHE_PROVIDER,
        query_signature=CACHE_SIGNATURE,
        response_body=payload,
        expires_at=expires_at,
    )
    stmt = stmt.on_conflict_do_update(
        index_elements=["provider", "query_signature"],
        set_={
            "response_body": stmt.excluded.response_body,
            "fetched_at": datetime.now(UTC),
            "expires_at": stmt.excluded.expires_at,
        },
    )
    session.execute(stmt)


def load_cost_index(
    session: Session | None = None,
    *,
    source: str = "auto",
    timeout: float = 15.0,
    snapshot_path: Path = SNAPSHOT_PATH,
    ttl: timedelta = DEFAULT_TTL,
    write_cache: bool = True,
) -> CostIndexResult:
    """Resolve WhereNext cost indices, following the ADR-005 fallback chain.

    ``source`` is one of ``auto`` (cache -> live -> snapshot), ``live``, ``cache``
    or ``snapshot``. When a live fetch succeeds and a ``session`` is given, the
    raw payload is cached in ``api_cache`` (unless ``write_cache=False``). The
    caller owns the transaction — this never commits.
    """
    if source not in VALID_SOURCES:
        raise ValueError(f"source must be one of {VALID_SOURCES}, got {source!r}")

    # Forced single-source paths.
    if source == "cache":
        if session is None:
            raise ValueError("source='cache' requires a database session")
        payload = _cache_get_fresh(session)
        if payload is None:
            raise LookupError("no fresh WhereNext row in api_cache")
        return CostIndexResult(parse_payload(payload), "cache", _meta(payload))
    if source == "snapshot":
        payload = load_snapshot(snapshot_path)
        return CostIndexResult(parse_payload(payload), "snapshot", _meta(payload))
    if source == "live":
        payload = fetch_live(timeout)
        mapping = parse_payload(payload)  # validate before caching
        if session is not None and write_cache:
            _cache_put(session, payload, ttl)
        return CostIndexResult(mapping, "live", _meta(payload))

    # source == "auto": cache -> live -> snapshot.
    if session is not None and write_cache:
        cached = _cache_get_fresh(session)
        if cached is not None:
            logger.info("WhereNext: using fresh api_cache row")
            return CostIndexResult(parse_payload(cached), "cache", _meta(cached))
    try:
        payload = fetch_live(timeout)
        mapping = parse_payload(payload)
        if session is not None and write_cache:
            _cache_put(session, payload, ttl)
        return CostIndexResult(mapping, "live", _meta(payload))
    except (urllib.error.URLError, TimeoutError, OSError, ValueError) as exc:
        logger.warning("WhereNext: live fetch failed (%s); falling back to snapshot", exc)
        payload = load_snapshot(snapshot_path)
        return CostIndexResult(parse_payload(payload), "snapshot", _meta(payload))


def _meta(payload: object) -> dict[str, object]:
    if isinstance(payload, dict) and isinstance(payload.get("meta"), dict):
        return dict(payload["meta"])
    return {}


class WhereNextProvider:
    """WhereNext as a :class:`~scripts.cost_of_living.CostOfLivingProvider`.

    Thin adapter over :func:`load_cost_index` so WhereNext plugs into
    :func:`~scripts.cost_of_living.resolve_cost_index` alongside any other source.
    Construction mirrors ``load_cost_index`` (session, ``source``, cache options);
    ``load()`` runs it and tags the provenance ``wherenext:<source>``.
    """

    name = "wherenext"

    def __init__(
        self,
        session: Session | None = None,
        *,
        source: str = "auto",
        timeout: float = 15.0,
        snapshot_path: Path = SNAPSHOT_PATH,
        ttl: timedelta = DEFAULT_TTL,
        write_cache: bool = True,
    ) -> None:
        self.session = session
        self.source = source
        self.timeout = timeout
        self.snapshot_path = snapshot_path
        self.ttl = ttl
        self.write_cache = write_cache

    def load(self) -> ProviderResult:
        result = load_cost_index(
            self.session,
            source=self.source,
            timeout=self.timeout,
            snapshot_path=self.snapshot_path,
            ttl=self.ttl,
            write_cache=self.write_cache,
        )
        return ProviderResult(result.mapping, f"wherenext:{result.source}", result.meta)
