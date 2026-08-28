"""Compute + store normalized destination_features (BS-031).

Turns the curated raw indicators in ``data/destination_features_raw.csv`` into
the normalized ``destination_features`` rows that drive ScoutAgent ranking
(technical documentation §8/§14). The methodology — raw sources, min-max
normalization, and the month-dependent season model — is documented in
``data/FEATURES_METHODOLOGY.md``.

Pipeline:

* **Read + validate** the raw CSV (one row per curated destination): five factor
  indices (culture/nature/nightlife/food/tourism), the ISO ``country_code`` join
  key, and a climate class. Malformed rows are logged and skipped, never fatal.
* **Cost-of-living** is resolved per country from **WhereNext** (BS-011) via
  ``scripts.wherenext`` and joined onto each destination by ``country_code`` —
  it is not a CSV column.
* **Normalize** every factor and the WhereNext cost index to ``[0,1]`` with
  min-max normalization across the destination set (§14.2). A constant column
  maps to ``0.5`` instead of dividing by zero.
* **Season score** is derived from the destination's climate class and the
  travel window: the mean over the window's months of that climate's monthly
  suitability curve, so the score varies by month.
* **Upsert** one row per destination (matched to a loaded ``destinations`` row on
  ``(city, country)``) keyed on ``destination_id``. Re-running refreshes in
  place — idempotent, never duplicated.

Cost-of-living is stored orientation-neutral (0 = cheapest, 1 = most expensive)
and inverted into a penalty by the ranking layer (BS-054), not here.

Usage (from ``backend/``)::

    python -m scripts.compute_destination_features                 # current month
    python -m scripts.compute_destination_features --month 7       # July window
    python -m scripts.compute_destination_features --month 6 --window-months 3
    python -m scripts.compute_destination_features --dry-run       # no writes
"""

from __future__ import annotations

import argparse
import csv
import logging
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import date
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path

from db.models import Destination, DestinationFeature
from db.session import SessionLocal, get_engine
from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from scripts import wherenext

logger = logging.getLogger(__name__)

# backend/scripts/compute_destination_features.py -> repo root is two parents up.
REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_RAW_CSV = REPO_ROOT / "data" / "destination_features_raw.csv"

# Raw factor columns that get min-max normalized straight into *_score columns.
FACTOR_COLUMNS = ("culture", "nature", "nightlife", "food", "tourism")
# cost_of_living is NOT a CSV column: it is resolved per country from WhereNext
# (scripts.wherenext) and joined on country_code. The CSV holds only the
# subjective factor indices, the join key, and the climate class.
REQUIRED_COLUMNS = ("city", "country", "country_code", *FACTOR_COLUMNS, "climate")

# Month-by-month travel suitability in [0,1] per climate class (Jan..Dec).
# Derived from Köppen climate character (see data/FEATURES_METHODOLOGY.md §3).
CLIMATE_PROFILES: dict[str, tuple[float, ...]] = {
    "mediterranean": (0.45, 0.45, 0.60, 0.75, 0.90, 0.95, 0.95, 0.95, 0.90, 0.80, 0.60, 0.45),
    "oceanic": (0.40, 0.40, 0.55, 0.70, 0.85, 0.90, 0.90, 0.90, 0.85, 0.70, 0.50, 0.40),
    "continental": (0.25, 0.30, 0.45, 0.65, 0.85, 0.95, 0.95, 0.90, 0.80, 0.60, 0.40, 0.30),
    "alpine": (0.30, 0.35, 0.45, 0.60, 0.80, 0.90, 0.95, 0.90, 0.80, 0.60, 0.45, 0.35),
    "baltic": (0.20, 0.20, 0.30, 0.50, 0.75, 0.90, 0.95, 0.90, 0.70, 0.50, 0.30, 0.20),
}

# Quantization for storage: NUMERIC(4,3) factor scores / NUMERIC(6,3) cost index.
_Q = Decimal("0.001")
_NEUTRAL = 0.5  # value assigned when a column is constant (max == min)


@dataclass
class RawRow:
    """One validated row of raw indicators for a destination."""

    city: str
    country: str
    country_code: str
    factors: dict[str, float]
    climate: str


@dataclass
class FeatureComputeSummary:
    """Outcome of a compute run, for logging and tests."""

    total: int = 0  # data rows read from the CSV
    valid: int = 0  # rows that passed validation
    skipped_rows: int = 0  # malformed CSV rows dropped
    matched: int = 0  # destinations found in the DB and upserted
    unmatched: int = 0  # valid rows with no destinations row (logged, skipped)
    months: tuple[int, ...] = ()
    cost_source: str = ""  # where WhereNext cost data came from (live/cache/snapshot)
    cost_countries: int = 0  # countries resolved from WhereNext
    cost_missing: int = 0  # valid rows whose country had no WhereNext cost_index
    skipped_reasons: list[str] = field(default_factory=list)


def _clean(value: str | None) -> str:
    return (value or "").strip()


def window_months(start_month: int, window: int) -> tuple[int, ...]:
    """Return the ``window`` consecutive months starting at ``start_month``.

    Months are 1-12 and wrap around the year end, so a 3-month window from
    November is ``(11, 12, 1)``.
    """
    if not 1 <= start_month <= 12:
        raise ValueError(f"month must be in 1..12, got {start_month}")
    if window < 1:
        raise ValueError(f"window-months must be >= 1, got {window}")
    return tuple(((start_month - 1 + offset) % 12) + 1 for offset in range(window))


def season_score(climate: str, months: Iterable[int]) -> float:
    """Mean travel suitability of ``climate`` over ``months`` (each 1-12).

    This is what makes ``season_score`` depend on the travel window: the same
    destination scores differently in summer vs. winter.
    """
    profile = CLIMATE_PROFILES[climate]
    months = tuple(months)
    if not months:
        raise ValueError("season_score requires at least one month")
    return sum(profile[m - 1] for m in months) / len(months)


def parse_raw_rows(reader: Iterable[dict[str, str]]) -> tuple[list[RawRow], list[str]]:
    """Validate raw CSV rows into ``RawRow`` records.

    Returns ``(rows, skip_reasons)``. Malformed rows (missing keys, non-numeric
    or negative indices, unknown climate class) are collected as reasons rather
    than raised, so one bad row never sinks the whole compute.
    """
    rows: list[RawRow] = []
    skip_reasons: list[str] = []

    # +2: CSV data starts on line 2 (line 1 is the header).
    for line_no, raw in enumerate(reader, start=2):
        city = _clean(raw.get("city"))
        country = _clean(raw.get("country"))
        if not city or not country:
            reason = f"line {line_no}: missing city/country ({city!r}, {country!r})"
            logger.warning("skipping row: %s", reason)
            skip_reasons.append(reason)
            continue

        country_code = _clean(raw.get("country_code")).upper()
        if len(country_code) != 2 or not country_code.isalpha():
            reason = f"line {line_no}: {city}: invalid country_code {raw.get('country_code')!r} (want ISO alpha-2)"
            logger.warning("skipping row: %s", reason)
            skip_reasons.append(reason)
            continue

        numeric: dict[str, float] = {}
        failed = False
        for column in FACTOR_COLUMNS:
            try:
                value = float(_clean(raw.get(column)))
            except ValueError:
                reason = f"line {line_no}: {city}: non-numeric {column} ({raw.get(column)!r})"
                logger.warning("skipping row: %s", reason)
                skip_reasons.append(reason)
                failed = True
                break
            if value < 0:
                reason = f"line {line_no}: {city}: negative {column} ({value})"
                logger.warning("skipping row: %s", reason)
                skip_reasons.append(reason)
                failed = True
                break
            numeric[column] = value
        if failed:
            continue

        climate = _clean(raw.get("climate")).lower()
        if climate not in CLIMATE_PROFILES:
            reason = (
                f"line {line_no}: {city}: unknown climate {climate!r} "
                f"(want one of {', '.join(sorted(CLIMATE_PROFILES))})"
            )
            logger.warning("skipping row: %s", reason)
            skip_reasons.append(reason)
            continue

        rows.append(
            RawRow(
                city=city,
                country=country,
                country_code=country_code,
                factors={col: numeric[col] for col in FACTOR_COLUMNS},
                climate=climate,
            )
        )

    return rows, skip_reasons


def read_raw_csv(path: Path) -> tuple[list[RawRow], list[str], int]:
    """Read + validate the raw indicators CSV. Returns ``(rows, reasons, total)``."""
    if not path.is_file():
        raise FileNotFoundError(f"raw destination features CSV not found: {path}")

    with path.open(encoding="utf-8", newline="") as fh:
        reader = csv.DictReader(fh)
        missing = set(REQUIRED_COLUMNS) - set(reader.fieldnames or [])
        if missing:
            raise ValueError(
                f"{path.name} is missing required column(s): {', '.join(sorted(missing))}"
            )
        raw_rows = list(reader)

    rows, skip_reasons = parse_raw_rows(raw_rows)
    return rows, skip_reasons, len(raw_rows)


def min_max_normalize(values: list[float]) -> list[float]:
    """Min-max normalize ``values`` to [0,1] (§14.2).

    A constant column (``max == min``) maps every value to ``0.5`` — a neutral
    score — rather than dividing by zero.
    """
    if not values:
        return []
    lo, hi = min(values), max(values)
    span = hi - lo
    if span == 0:
        return [_NEUTRAL] * len(values)
    return [(v - lo) / span for v in values]


def _q(value: float) -> Decimal:
    """Quantize a [0,1] float to 3 decimal places for storage."""
    return Decimal(str(value)).quantize(_Q, rounding=ROUND_HALF_UP)


def compute_features(
    rows: list[RawRow],
    months: Iterable[int],
    cost_by_country: dict[str, float],
) -> dict[tuple[str, str], dict[str, Decimal | None]]:
    """Normalize ``rows`` into per-destination feature dicts, all scores in [0,1].

    Keyed by ``(city, country)``. The five ``*_score`` columns are min-max
    normalized across the set; ``season_score`` comes from the climate/month
    model; ``cost_of_living`` is the destination's WhereNext country cost index
    (looked up in ``cost_by_country`` by ISO code) min-max normalized across the
    destinations that have one. A destination whose country is absent from
    ``cost_by_country`` gets ``cost_of_living = None`` (logged by the caller).
    Empty input yields an empty mapping.
    """
    months = tuple(months)
    if not rows:
        return {}

    normalized_factors = {
        col: min_max_normalize([r.factors[col] for r in rows]) for col in FACTOR_COLUMNS
    }

    # Cost: normalize across only the destinations WhereNext actually covers, so
    # a missing country never distorts the [0,1] range for the rest.
    raw_costs = [cost_by_country.get(r.country_code) for r in rows]
    present_norm = iter(min_max_normalize([c for c in raw_costs if c is not None]))
    normalized_cost = [None if c is None else next(present_norm) for c in raw_costs]

    features: dict[tuple[str, str], dict[str, Decimal | None]] = {}
    for idx, row in enumerate(rows):
        cost = normalized_cost[idx]
        record: dict[str, Decimal | None] = {
            "cost_of_living": _q(cost) if cost is not None else None
        }
        for col in FACTOR_COLUMNS:
            record[f"{col}_score"] = _q(normalized_factors[col][idx])
        record["season_score"] = _q(season_score(row.climate, months))
        features[(row.city, row.country)] = record
    return features


def upsert_features(
    session: Session, features: dict[tuple[str, str], dict[str, Decimal | None]]
) -> tuple[int, int]:
    """Upsert computed features into ``destination_features``.

    Matches each ``(city, country)`` to a loaded ``destinations`` row to resolve
    ``destination_id`` (the target primary key). Rows without a matching
    destination are logged and skipped. Uses ``ON CONFLICT DO UPDATE`` so a
    re-run refreshes the scores (and ``updated_at``) in place.

    Returns ``(matched, unmatched)``.
    """
    if not features:
        return 0, 0

    id_by_key = {
        (city, country): dest_id
        for dest_id, city, country in session.execute(
            select(Destination.id, Destination.city, Destination.country)
        ).all()
    }

    records: list[dict[str, object]] = []
    unmatched = 0
    for (city, country), record in features.items():
        dest_id = id_by_key.get((city, country))
        if dest_id is None:
            logger.warning(
                "skipping features for %s, %s: no matching destinations row "
                "(load it via scripts.ingest_destinations first)",
                city,
                country,
            )
            unmatched += 1
            continue
        records.append({"destination_id": dest_id, **record})

    if not records:
        return 0, unmatched

    stmt = pg_insert(DestinationFeature).values(records)
    stmt = stmt.on_conflict_do_update(
        index_elements=["destination_id"],
        set_={
            "cost_of_living": stmt.excluded.cost_of_living,
            "culture_score": stmt.excluded.culture_score,
            "nature_score": stmt.excluded.nature_score,
            "nightlife_score": stmt.excluded.nightlife_score,
            "food_score": stmt.excluded.food_score,
            "tourism_score": stmt.excluded.tourism_score,
            "season_score": stmt.excluded.season_score,
            "updated_at": func.now(),
        },
    )
    session.execute(stmt)
    return len(records), unmatched


def compute_and_store(
    session: Session,
    path: Path = DEFAULT_RAW_CSV,
    *,
    months: Iterable[int] = (),
    cost_source: str = "auto",
    dry_run: bool = False,
) -> FeatureComputeSummary:
    """Compute features from ``path`` and upsert them through ``session``.

    ``months`` is the travel window (each 1-12); it defaults to the current
    month. ``cost_source`` selects where WhereNext cost data is read from
    (``auto`` / ``live`` / ``cache`` / ``snapshot``; see ``scripts.wherenext``).
    The caller owns the transaction: this flushes but does not commit, so tests
    can roll back and the CLI can commit once at the end.
    """
    months = tuple(months) or (date.today().month,)
    rows, skip_reasons, total = read_raw_csv(path)

    summary = FeatureComputeSummary(
        total=total,
        valid=len(rows),
        skipped_rows=len(skip_reasons),
        months=months,
        skipped_reasons=skip_reasons,
    )

    # Resolve WhereNext cost-of-living. On a dry run we pass no session so the
    # cache is neither read nor written (auto still tries live, then snapshot).
    cost = wherenext.load_cost_index(
        None if dry_run else session, source=cost_source, write_cache=not dry_run
    )
    summary.cost_source = cost.source
    summary.cost_countries = cost.count
    summary.cost_missing = sum(1 for r in rows if r.country_code not in cost.mapping)

    features = compute_features(rows, months, cost.mapping)
    if dry_run:
        summary.matched = len(features)  # nothing resolved against the DB
        return summary

    matched, unmatched = upsert_features(session, features)
    session.flush()
    summary.matched = matched
    summary.unmatched = unmatched
    return summary


def _configure_logging() -> None:
    if not logging.getLogger().handlers:
        logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Compute and store normalized destination features."
    )
    parser.add_argument(
        "--csv",
        type=Path,
        default=DEFAULT_RAW_CSV,
        help=f"path to the raw indicators CSV (default: {DEFAULT_RAW_CSV})",
    )
    parser.add_argument(
        "--month",
        type=int,
        default=date.today().month,
        help="first month of the travel window, 1-12 (default: current month)",
    )
    parser.add_argument(
        "--window-months",
        type=int,
        default=1,
        help="number of consecutive months in the travel window (default: 1)",
    )
    parser.add_argument(
        "--cost-source",
        choices=wherenext.VALID_SOURCES,
        default="auto",
        help="where WhereNext cost data comes from: auto (default), live, cache, snapshot",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="compute and report without writing to the database",
    )
    args = parser.parse_args(argv)

    _configure_logging()

    try:
        months = window_months(args.month, args.window_months)
    except ValueError as exc:
        logger.error("invalid travel window: %s", exc)
        return 1

    try:
        session = SessionLocal(bind=get_engine())
        try:
            summary = compute_and_store(
                session, args.csv, months=months, cost_source=args.cost_source, dry_run=args.dry_run
            )
            if not args.dry_run:
                session.commit()
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()
    except (FileNotFoundError, ValueError, LookupError) as exc:
        logger.error("feature computation aborted: %s", exc)
        return 1

    window_desc = ",".join(str(m) for m in summary.months)
    cost_desc = (
        f"cost from WhereNext/{summary.cost_source} "
        f"({summary.cost_countries} countries, {summary.cost_missing} uncovered)"
    )
    if args.dry_run:
        logger.info(
            "dry-run: %d row(s) read, %d valid, %d skipped, %d computed "
            "for months [%s]; %s (no writes)",
            summary.total,
            summary.valid,
            summary.skipped_rows,
            summary.matched,
            window_desc,
            cost_desc,
        )
    else:
        logger.info(
            "features computed: %d read, %d valid, %d skipped - "
            "%d upserted, %d unmatched for months [%s]; %s",
            summary.total,
            summary.valid,
            summary.skipped_rows,
            summary.matched,
            summary.unmatched,
            window_desc,
            cost_desc,
        )
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
