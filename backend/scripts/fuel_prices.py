"""Fuel-price provider — EU Oil Bulletin + fuel-prices.eu live feed (BS-032 / BS-003).

Resolves per-country consumer fuel prices (Euro-95 petrol + diesel, tax-inclusive,
EUR/L) for the countries BudgetSwarm cares about, following the sourcing decision
recorded in ADR-009 and docs/verification-checklist.md §"Fuel prices":

* **EU-27** — the authoritative, citable prices come from the **EU Weekly Oil
  Bulletin** (European Commission, DG ENER), which BudgetSwarm ingests in
  practice from **fuel-prices.eu**'s machine-readable export (``llms-full.txt``).
  The export republishes the bulletin as CC BY 4.0 data; we parse its
  "CURRENT FUEL PRICES — ALL COUNTRIES" table (``quality`` = real).
* **Non-EU neighbours** (MK, RS, BA, ME, TR …) — the bulletin covers EU-27 only,
  so for the neighbouring countries that appear as destinations/origins we ingest
  the **real** station-averaged price from fuel-prices.eu's separate ``/live/``
  summary feed (a different, station-open-data methodology, ~12–18 h fresh).
  A country the live feed does not cover (e.g. AL) falls back to the EU average.
* **``EU`` synthetic row** — the EU-27 average petrol/diesel is stored under the
  two-letter code ``EU`` as the documented ultimate fallback for any origin
  country with no per-country price of its own (``quality`` = estimated).

Following ADR-008 ("cache into Postgres rather than call live") the app never
calls these endpoints at request time: this module fetches once, the ingestion
CLI (``scripts.ingest_fuel_prices``) writes the results into ``fuel_prices``, and
everything downstream reads the table. Committed snapshots under ``data/`` are the
always-available offline/CI floor so parsing stays reproducible without network.

Attribution (thesis + app): "fuel-prices.eu — data from the European Commission
Weekly Oil Bulletin (CC BY 4.0)".
"""

from __future__ import annotations

import json
import logging
import re
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path
from typing import Protocol, runtime_checkable

logger = logging.getLogger(__name__)

# backend/scripts/fuel_prices.py -> repo root is two parents up.
REPO_ROOT = Path(__file__).resolve().parents[2]
BULLETIN_SNAPSHOT = REPO_ROOT / "data" / "fuel_prices_oil_bulletin.txt"
LIVE_SNAPSHOT = REPO_ROOT / "data" / "fuel_prices_live_summary.json"

# fuel-prices.eu machine-readable endpoints (see docs/verification-checklist.md).
BULLETIN_URL = "https://www.fuel-prices.eu/llms-full.txt"
LIVE_SUMMARY_URL = "https://www.fuel-prices.eu/live/api.php?action=summary"

# Provenance labels written to fuel_prices.source (<= 80 chars).
SOURCE_BULLETIN = "EU Weekly Oil Bulletin"
SOURCE_LIVE = "fuel-prices.eu live (station avg)"
SOURCE_EU_AVG = "EU Weekly Oil Bulletin (EU avg)"

# The two-letter code the EU-average row is stored under (fuel_prices PK is CHAR(2)).
EU_AVG_CODE = "EU"

VALID_SOURCES = ("auto", "live", "snapshot")

# Sane consumer price bounds (EUR/L, tax-incl.) — guards against a mangled parse
# or a bad upstream value. The 2026 EU range is ~€1.21–2.46, so this is generous.
_PRICE_MIN, _PRICE_MAX = 0.3, 5.0

# Country codes BudgetSwarm needs a per-country row for: the origin (MK) plus
# every destination country in data/curated_destinations.csv. The EU-27 members
# here are normally supplied by the bulletin; the non-EU neighbours (and any EU
# member the bulletin happens to omit, e.g. GR in some weeks) are filled from the
# /live/ feed, else the EU average. Kept as a static, documented set so fuel
# ingestion has no hard dependency on the destinations CSV existing.
DEFAULT_TARGETS = (
    "AT",
    "BG",
    "CZ",
    "EE",
    "ES",
    "GR",
    "HR",
    "HU",
    "IT",
    "LT",
    "LV",
    "PL",
    "PT",
    "RO",
    "SI",
    "SK",  # EU destination countries
    "AL",
    "BA",
    "ME",
    "MK",
    "RS",
    "TR",  # non-EU neighbours (incl. MK origin)
)


@dataclass
class FuelRecord:
    """One upsertable ``fuel_prices`` row."""

    country_code: str  # ISO alpha-2 (or the synthetic "EU")
    petrol_e95: float | None
    diesel: float | None
    source: str
    price_date: date


@dataclass
class BulletinResult:
    """Parsed EU Oil Bulletin export."""

    prices: dict[str, tuple[float | None, float | None]]  # cc -> (petrol, diesel)
    eu_average: tuple[float | None, float | None]  # (petrol, diesel)
    price_date: date
    source: str  # "live" | "snapshot"
    meta: dict[str, object] = field(default_factory=dict)


@dataclass
class LiveResult:
    """Parsed fuel-prices.eu /live/ summary."""

    prices: dict[str, tuple[float | None, float | None, date]]  # cc -> (petrol, diesel, fetched)
    source: str  # "live" | "snapshot"


def _valid_price(value: float | None) -> float | None:
    """Return ``value`` if it is a sane fuel price, else ``None`` (logged)."""
    if value is None:
        return None
    if _PRICE_MIN <= value <= _PRICE_MAX:
        return value
    logger.warning(
        "fuel: dropping out-of-range price %.3f (bounds %.1f-%.1f)", value, _PRICE_MIN, _PRICE_MAX
    )
    return None


# --- EU Oil Bulletin export (llms-full.txt) --------------------------------

# One row of the "ALL COUNTRIES" table, e.g.:
#   MT   Malta                  €     1.340 €     1.210 $5.85 ...
_ROW_RE = re.compile(r"^([A-Z]{2})\s+.+?€\s*([0-9]+\.[0-9]+)\s+€\s*([0-9]+\.[0-9]+)\s+\$")
_EU_PETROL_RE = re.compile(r"EU Average Euro 95 Petrol:\s*€\s*([0-9]+\.[0-9]+)")
_EU_DIESEL_RE = re.compile(r"EU Average Diesel:\s*€\s*([0-9]+\.[0-9]+)")
_DATA_DATE_RE = re.compile(r"^data_date:\s*([0-9]{4}-[0-9]{2}-[0-9]{2})", re.MULTILINE)


def parse_bulletin_export(text: str) -> BulletinResult:
    """Parse the fuel-prices.eu ``llms-full.txt`` export into per-country prices.

    Reads the "CURRENT FUEL PRICES — ALL COUNTRIES" table (EU-27, petrol + diesel),
    the published EU-27 average, and the bulletin's ``data_date``. Raises
    ``ValueError`` if the shape is wrong or no usable country row is found.
    """
    date_match = _DATA_DATE_RE.search(text)
    if not date_match:
        raise ValueError("bulletin export has no parseable data_date")
    price_date = date.fromisoformat(date_match.group(1))

    prices: dict[str, tuple[float | None, float | None]] = {}
    for line in text.splitlines():
        row = _ROW_RE.match(line)
        if not row:
            continue
        code = row.group(1)
        petrol = _valid_price(float(row.group(2)))
        diesel = _valid_price(float(row.group(3)))
        if petrol is None and diesel is None:
            continue
        prices[code] = (petrol, diesel)

    if not prices:
        raise ValueError("bulletin export contained no usable country price rows")

    eu_petrol = _EU_PETROL_RE.search(text)
    eu_diesel = _EU_DIESEL_RE.search(text)
    eu_average = (
        _valid_price(float(eu_petrol.group(1))) if eu_petrol else None,
        _valid_price(float(eu_diesel.group(1))) if eu_diesel else None,
    )
    return BulletinResult(prices, eu_average, price_date, "?", {"countries": len(prices)})


def fetch_bulletin_live(timeout: float = 20.0) -> str:
    """GET the live fuel-prices.eu bulletin export (raises on network error)."""
    logger.info("fuel: fetching bulletin export from %s", BULLETIN_URL)
    request = urllib.request.Request(BULLETIN_URL, headers={"Accept": "text/plain"})
    with urllib.request.urlopen(request, timeout=timeout) as response:  # noqa: S310 (https only)
        return response.read().decode("utf-8")


def load_bulletin_snapshot(path: Path = BULLETIN_SNAPSHOT) -> str:
    """Load the committed bulletin-export snapshot."""
    if not path.is_file():
        raise FileNotFoundError(f"fuel bulletin snapshot not found: {path}")
    return path.read_text(encoding="utf-8")


def load_bulletin(
    *, source: str = "auto", timeout: float = 20.0, snapshot_path: Path = BULLETIN_SNAPSHOT
) -> BulletinResult:
    """Resolve the EU Oil Bulletin export (``auto`` = live then snapshot)."""
    if source not in VALID_SOURCES:
        raise ValueError(f"source must be one of {VALID_SOURCES}, got {source!r}")
    if source == "snapshot":
        result = parse_bulletin_export(load_bulletin_snapshot(snapshot_path))
        result.source = "snapshot"
        return result
    if source == "live":
        result = parse_bulletin_export(fetch_bulletin_live(timeout))
        result.source = "live"
        return result
    # auto: live -> snapshot
    try:
        result = parse_bulletin_export(fetch_bulletin_live(timeout))
        result.source = "live"
        return result
    except (urllib.error.URLError, TimeoutError, OSError, ValueError) as exc:
        logger.warning("fuel: bulletin live fetch failed (%s); using snapshot", exc)
        result = parse_bulletin_export(load_bulletin_snapshot(snapshot_path))
        result.source = "snapshot"
        return result


# --- fuel-prices.eu /live/ summary feed ------------------------------------


def _fetched_date(raw: object) -> date:
    """Best-effort parse of a ``last_fetched`` timestamp into a date."""
    text = str(raw or "").strip()
    if not text:
        return datetime.now().date()
    try:
        return datetime.fromisoformat(text.replace("Z", "")).date()
    except ValueError:
        return date.fromisoformat(text[:10])


def parse_live_summary(payload: object) -> dict[str, tuple[float | None, float | None, date]]:
    """Extract ``cc -> (sp95, diesel, fetched_date)`` from the /live/ summary JSON.

    Malformed country records are logged and skipped. Raises ``ValueError`` if the
    payload shape is wrong.
    """
    if not isinstance(payload, dict) or not isinstance(payload.get("data"), dict):
        raise ValueError("live summary payload is not an object with a 'data' block")
    countries = payload["data"].get("countries")
    if not isinstance(countries, list):
        raise ValueError("live summary payload has no 'countries' array")

    out: dict[str, tuple[float | None, float | None, date]] = {}
    for record in countries:
        if not isinstance(record, dict):
            continue
        code = str(record.get("country_code", "")).strip().upper()
        if len(code) != 2 or not code.isalpha():
            continue
        fuels = record.get("fuels") or {}
        petrol = _valid_price(_avg(fuels.get("sp95")))
        diesel = _valid_price(_avg(fuels.get("diesel")))
        if petrol is None and diesel is None:
            continue
        out[code] = (petrol, diesel, _fetched_date(record.get("last_fetched")))
    return out


def _avg(fuel_block: object) -> float | None:
    if isinstance(fuel_block, dict):
        try:
            return float(fuel_block.get("avg"))  # type: ignore[arg-type]
        except (TypeError, ValueError):
            return None
    return None


def fetch_live_summary(timeout: float = 20.0) -> dict[str, object]:
    """GET the live fuel-prices.eu /live/ summary payload (raises on network error)."""
    logger.info("fuel: fetching live station summary from %s", LIVE_SUMMARY_URL)
    request = urllib.request.Request(LIVE_SUMMARY_URL, headers={"Accept": "application/json"})
    with urllib.request.urlopen(request, timeout=timeout) as response:  # noqa: S310 (https only)
        payload = json.load(response)
    if not isinstance(payload, dict):
        raise ValueError("live summary response was not a JSON object")
    return payload


def load_live_snapshot(path: Path = LIVE_SNAPSHOT) -> dict[str, object]:
    """Load the committed /live/ summary snapshot payload."""
    if not path.is_file():
        raise FileNotFoundError(f"fuel live snapshot not found: {path}")
    with path.open(encoding="utf-8") as fh:
        payload = json.load(fh)
    if not isinstance(payload, dict):
        raise ValueError(f"fuel live snapshot {path.name} is not a JSON object")
    return payload


def load_live(
    *, source: str = "auto", timeout: float = 20.0, snapshot_path: Path = LIVE_SNAPSHOT
) -> LiveResult:
    """Resolve the /live/ summary feed (``auto`` = live then snapshot)."""
    if source not in VALID_SOURCES:
        raise ValueError(f"source must be one of {VALID_SOURCES}, got {source!r}")
    if source == "snapshot":
        return LiveResult(parse_live_summary(load_live_snapshot(snapshot_path)), "snapshot")
    if source == "live":
        return LiveResult(parse_live_summary(fetch_live_summary(timeout)), "live")
    try:
        return LiveResult(parse_live_summary(fetch_live_summary(timeout)), "live")
    except (urllib.error.URLError, TimeoutError, OSError, ValueError) as exc:
        logger.warning("fuel: live summary fetch failed (%s); using snapshot", exc)
        return LiveResult(parse_live_summary(load_live_snapshot(snapshot_path)), "snapshot")


# --- pluggable provider layer ----------------------------------------------
#
# Adding a new fuel source later (e.g. the US EIA, or an Asian price feed) means
# writing one small class that implements ``FuelPriceProvider`` and dropping it
# into the provider list — nothing in the resolver, the ingest CLI, or the schema
# has to change. A provider yields ``country_code -> FuelDatum`` for whatever it
# covers; the resolver merges providers by priority (first to supply a country
# wins), decides which countries to emit, and fills any still-missing target from
# an optional :class:`FuelFallback`. The default wiring (EU Oil Bulletin + the
# fuel-prices.eu /live/ feed + an EU-average fallback) reproduces the BS-032
# behaviour exactly; a future region would add its own provider(s) and, if its
# origins live outside the EU, its own fallback (the EU average is Euro-centric).


@dataclass
class FuelDatum:
    """A single country's price point from a provider."""

    petrol_e95: float | None
    diesel: float | None
    price_date: date


@dataclass
class ProviderResult:
    """What a :class:`FuelPriceProvider` returns from ``load()``."""

    prices: dict[str, FuelDatum]  # ISO alpha-2 country code -> datum
    loaded_from: str  # how it was obtained, e.g. "live" | "snapshot"
    # Optional EU-27 average + its date, exposed by the bulletin provider so the
    # EU-average fallback can reuse it. Other providers leave these ``None``.
    regional_average: tuple[float | None, float | None] | None = None
    average_date: date | None = None
    meta: dict[str, object] = field(default_factory=dict)


@runtime_checkable
class FuelPriceProvider(Protocol):
    """The contract every fuel source implements.

    ``name`` is a short identifier used in provenance; ``source_label`` is written
    to ``fuel_prices.source``; ``emit_all`` decides scope — ``True`` means "ingest
    every country this provider returns" (an authoritative regional core, like the
    EU Oil Bulletin), ``False`` means "only use it to fill requested target
    countries" (a supplementary feed). ``load()`` returns a :class:`ProviderResult`
    or raises; the resolver logs and skips a provider that raises (degrade, never
    crash — ADR-005).
    """

    name: str
    source_label: str
    emit_all: bool

    def load(self) -> ProviderResult: ...


class OilBulletinProvider:
    """EU Weekly Oil Bulletin export (EU-27) — the authoritative, citable core."""

    name = "bulletin"
    source_label = SOURCE_BULLETIN
    emit_all = True

    def __init__(
        self,
        *,
        source: str = "auto",
        timeout: float = 20.0,
        snapshot_path: Path = BULLETIN_SNAPSHOT,
    ) -> None:
        self.source = source
        self.timeout = timeout
        self.snapshot_path = snapshot_path

    def load(self) -> ProviderResult:
        b = load_bulletin(
            source=self.source, timeout=self.timeout, snapshot_path=self.snapshot_path
        )
        prices = {
            code: FuelDatum(petrol, diesel, b.price_date)
            for code, (petrol, diesel) in b.prices.items()
        }
        return ProviderResult(
            prices,
            b.source,
            regional_average=b.eu_average,
            average_date=b.price_date,
            meta=b.meta,
        )


class LiveStationProvider:
    """fuel-prices.eu /live/ station feed — supplementary, fills target countries."""

    name = "live"
    source_label = SOURCE_LIVE
    emit_all = False

    def __init__(
        self, *, source: str = "auto", timeout: float = 20.0, snapshot_path: Path = LIVE_SNAPSHOT
    ) -> None:
        self.source = source
        self.timeout = timeout
        self.snapshot_path = snapshot_path

    def load(self) -> ProviderResult:
        lv = load_live(source=self.source, timeout=self.timeout, snapshot_path=self.snapshot_path)
        prices = {
            code: FuelDatum(petrol, diesel, fetched)
            for code, (petrol, diesel, fetched) in lv.prices.items()
        }
        return ProviderResult(prices, lv.source)


@dataclass
class ResolveContext:
    """Cross-provider context a fallback may draw on (e.g. the regional average)."""

    regional_average: tuple[float | None, float | None] | None
    average_date: date | None


@runtime_checkable
class FuelFallback(Protocol):
    """Fills targets no provider covered, and may add synthetic aggregate rows."""

    provenance: str
    source_label: str

    def datum_for(self, code: str, ctx: ResolveContext) -> FuelDatum | None: ...

    def synthetic_records(self, ctx: ResolveContext) -> list[FuelRecord]: ...


@dataclass
class EuAverageFallback:
    """Fill uncovered targets with the EU-27 average, and emit the ``EU`` row.

    Euro-centric by design (it uses the bulletin's EU average). A future non-EU
    rollout would supply a different fallback — a regional average, or ``None`` to
    leave uncovered countries out entirely.
    """

    source_label: str = SOURCE_EU_AVG
    synthetic_code: str | None = EU_AVG_CODE
    provenance: str = "eu-average"

    def datum_for(self, code: str, ctx: ResolveContext) -> FuelDatum | None:
        if ctx.regional_average is None:
            return None
        petrol, diesel = ctx.regional_average
        return FuelDatum(petrol, diesel, ctx.average_date or date.today())

    def synthetic_records(self, ctx: ResolveContext) -> list[FuelRecord]:
        if self.synthetic_code is None or ctx.regional_average is None:
            return []
        petrol, diesel = ctx.regional_average
        return [
            FuelRecord(
                self.synthetic_code,
                petrol,
                diesel,
                self.source_label,
                ctx.average_date or date.today(),
            )
        ]


@dataclass
class ResolvedFuel:
    """The full set of records to upsert plus a provenance breakdown."""

    records: list[FuelRecord]
    provenance: dict[str, str]  # country_code -> source label
    provider_sources: dict[str, str]  # provider name -> how it loaded ("live"/"snapshot")
    price_date: date | None  # representative date (first provider that carried one)


def default_providers(
    *,
    source: str = "auto",
    timeout: float = 20.0,
    bulletin_snapshot: Path = BULLETIN_SNAPSHOT,
    live_snapshot: Path = LIVE_SNAPSHOT,
) -> list[FuelPriceProvider]:
    """The BS-032 default chain: EU Oil Bulletin (priority) then the /live/ feed."""
    return [
        OilBulletinProvider(source=source, timeout=timeout, snapshot_path=bulletin_snapshot),
        LiveStationProvider(source=source, timeout=timeout, snapshot_path=live_snapshot),
    ]


# Sentinel so ``fallback=None`` (explicitly no fallback) is distinct from "unset".
_DEFAULT_FALLBACK: object = object()


def resolve_from_providers(
    providers: list[FuelPriceProvider],
    *,
    targets: tuple[str, ...] = DEFAULT_TARGETS,
    fallback: FuelFallback | None | object = _DEFAULT_FALLBACK,
) -> ResolvedFuel:
    """Merge ``providers`` (priority order) into the rows to upsert.

    First provider to supply a country wins. Countries emitted = everything an
    ``emit_all`` provider returns (authoritative regional cores) plus every
    ``target``. A target no provider covers is filled by ``fallback`` (default:
    the EU average), which may also append synthetic aggregate rows (the ``EU``
    row). A provider that raises is logged and skipped.
    """
    if fallback is _DEFAULT_FALLBACK:
        fallback = EuAverageFallback()

    loaded: list[tuple[FuelPriceProvider, ProviderResult]] = []
    provider_sources: dict[str, str] = {}
    ctx = ResolveContext(regional_average=None, average_date=None)

    for provider in providers:
        try:
            result = provider.load()
        except Exception as exc:  # noqa: BLE001 - degrade, never crash (ADR-005)
            logger.warning("fuel: provider %s failed: %s", getattr(provider, "name", provider), exc)
            continue
        loaded.append((provider, result))
        provider_sources[provider.name] = result.loaded_from
        if ctx.regional_average is None and result.regional_average is not None:
            ctx = ResolveContext(result.regional_average, result.average_date)

    # Merge (first provider wins) and work out which countries to emit.
    merged: dict[str, tuple[FuelDatum, FuelPriceProvider]] = {}
    emit: set[str] = {c.upper() for c in targets}
    for provider, result in loaded:
        if provider.emit_all:
            emit |= set(result.prices)
        for code, datum in result.prices.items():
            merged.setdefault(code, (datum, provider))

    records: list[FuelRecord] = []
    provenance: dict[str, str] = {}
    fb = fallback if fallback is not _DEFAULT_FALLBACK else None

    for code in sorted(emit):
        if code in merged:
            datum, provider = merged[code]
            records.append(
                FuelRecord(
                    code, datum.petrol_e95, datum.diesel, provider.source_label, datum.price_date
                )
            )
            provenance[code] = f"{provider.name}:{provider_sources[provider.name]}"
        elif isinstance(fb, FuelFallback):
            datum = fb.datum_for(code, ctx)
            if datum is None:
                logger.warning("fuel: no price and no fallback for %s - skipping", code)
                continue
            records.append(
                FuelRecord(code, datum.petrol_e95, datum.diesel, fb.source_label, datum.price_date)
            )
            provenance[code] = fb.provenance
            logger.info("fuel: %s covered by no provider - using %s", code, fb.provenance)
        else:
            logger.warning("fuel: no price and no fallback for %s - skipping", code)

    if isinstance(fb, FuelFallback):
        for record in fb.synthetic_records(ctx):
            records.append(record)
            provenance[record.country_code] = fb.provenance

    return ResolvedFuel(records, provenance, provider_sources, ctx.average_date)


def resolve_fuel_records(
    *,
    targets: tuple[str, ...] = DEFAULT_TARGETS,
    source: str = "auto",
    timeout: float = 20.0,
    bulletin_snapshot: Path = BULLETIN_SNAPSHOT,
    live_snapshot: Path = LIVE_SNAPSHOT,
) -> ResolvedFuel:
    """Convenience wrapper: resolve using the default BS-032 provider chain.

    Equivalent to ``resolve_from_providers(default_providers(...))`` with the
    EU-average fallback. Callers that want a custom source mix (e.g. adding a US
    provider) call :func:`resolve_from_providers` with their own provider list.
    """
    providers = default_providers(
        source=source,
        timeout=timeout,
        bulletin_snapshot=bulletin_snapshot,
        live_snapshot=live_snapshot,
    )
    return resolve_from_providers(providers, targets=targets)
