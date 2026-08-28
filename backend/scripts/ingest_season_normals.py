"""Ingest per-city seasonal suitability from Open-Meteo (BS-031).

Replaces the hand-drawn climate-archetype season curves with **real per-city
climate normals from Open-Meteo** (free, no API key). For each destination it
pulls a multi-year daily history from the Open-Meteo archive, aggregates it to
twelve monthly normals (mean temperature and mean monthly precipitation), and
converts each month into a travel-suitability score in [0,1] via a documented
comfort function. ``scripts.compute_destination_features`` then reads these
curves so ``season_score`` for a travel window is real, per-city, and
month-dependent (see ``data/FEATURES_METHODOLOGY.md`` §3).

Comfort function (per month): a temperature score peaking at a comfortable travel
temperature, combined with a precipitation penalty --

    temp_score   = exp( -(mean_temp - IDEAL_TEMP)^2 / (2 * TEMP_SIGMA^2) )
    precip_score = max(0, 1 - mean_monthly_precip_mm / PRECIP_ZERO_SCORE_MM)
    suitability  = TEMP_WEIGHT * temp_score + PRECIP_WEIGHT * precip_score

Deriving suitability from real temperature/precipitation also removes the old
Northern-Hemisphere assumption automatically: a Southern-Hemisphere city's data
already peaks in its own summer.

Output (committed, so the pipeline is reproducible offline/in CI):

* ``data/season_profiles.json`` -- per destination: the 12 monthly ``temp_c``,
  ``precip_mm`` and ``suitability`` values, plus a ``meta`` block (source,
  licence, normal period, comfort-function parameters).

Open-Meteo data is licensed CC BY 4.0 (ERA5 reanalysis). No API key is required.

Usage (from ``backend/``)::

    python -m scripts.ingest_season_normals                    # all destinations
    python -m scripts.ingest_season_normals --city Sofia       # one city (merged in)
    python -m scripts.ingest_season_normals --start 2014 --end 2023
    python -m scripts.ingest_season_normals --dry-run          # fetch + report, no writes
"""

from __future__ import annotations

import argparse
import csv
import json
import logging
import math
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

logger = logging.getLogger(__name__)

# backend/scripts/ingest_season_normals.py -> repo root is two parents up.
REPO_ROOT = Path(__file__).resolve().parents[2]
DESTINATIONS_CSV = REPO_ROOT / "data" / "curated_destinations.csv"
SNAPSHOT_PATH = REPO_ROOT / "data" / "season_profiles.json"

ARCHIVE_URL = "https://archive-api.open-meteo.com/v1/archive"
USER_AGENT = "BudgetSwarm/1.0 (academic thesis project; Open-Meteo climate ingestion)"
DEFAULT_START_YEAR = 2014
DEFAULT_END_YEAR = 2023
DEFAULT_SLEEP_S = 1.0

# Comfort-function parameters (documented in data/FEATURES_METHODOLOGY.md §3).
IDEAL_TEMP_C = 20.0  # most comfortable travel temperature
TEMP_SIGMA_C = 9.0  # spread: ~0.6 at 11 C / 29 C, ~0.2 at 2 C / 38 C
PRECIP_ZERO_SCORE_MM = 200.0  # monthly precip at/above which the rain score is 0
TEMP_WEIGHT = 0.7
PRECIP_WEIGHT = 0.3

MONTHS = range(1, 13)


@dataclass
class SeasonIngestSummary:
    total: int = 0
    fetched: int = 0
    failed: int = 0
    period: str = ""
    failures: list[str] = field(default_factory=list)


@dataclass
class MergeReport:
    refreshed: list[str] = field(default_factory=list)
    kept: list[str] = field(default_factory=list)
    missing: list[str] = field(default_factory=list)


def month_suitability(temp_c: float, precip_mm: float) -> float:
    """Travel-suitability in [0,1] for a month's mean temp (C) and precip (mm)."""
    temp_score = math.exp(-((temp_c - IDEAL_TEMP_C) ** 2) / (2 * TEMP_SIGMA_C**2))
    precip_score = max(0.0, 1.0 - precip_mm / PRECIP_ZERO_SCORE_MM)
    return TEMP_WEIGHT * temp_score + PRECIP_WEIGHT * precip_score


def read_destinations(path: Path = DESTINATIONS_CSV) -> list[dict[str, str]]:
    """Read curated destinations (needs city, country, lat, lon)."""
    if not path.is_file():
        raise FileNotFoundError(f"destinations CSV not found: {path}")
    with path.open(encoding="utf-8", newline="") as fh:
        rows = list(csv.DictReader(fh))
    if not rows:
        raise ValueError(f"{path.name} has no rows")
    return rows


def monthly_normals(
    times: list[str], temps: list[float | None], precs: list[float | None]
) -> tuple[list[float], list[float]]:
    """Aggregate a daily series to 12 monthly normals: (mean_temp, mean_precip).

    ``mean_temp[m]`` is the average daily mean temperature in month ``m``.
    ``mean_precip[m]`` is the average of the per-year monthly precipitation totals
    (so it reads as "typical mm in that month").
    """
    temp_sum: dict[int, float] = defaultdict(float)
    temp_n: dict[int, int] = defaultdict(int)
    precip_by_year: dict[tuple[int, int], float] = defaultdict(float)

    for day, temp, precip in zip(times, temps, precs, strict=True):
        month = int(day[5:7])
        if temp is not None:
            temp_sum[month] += temp
            temp_n[month] += 1
        if precip is not None:
            year = int(day[:4])
            precip_by_year[(year, month)] += precip

    precip_lists: dict[int, list[float]] = defaultdict(list)
    for (_year, month), total in precip_by_year.items():
        precip_lists[month].append(total)

    mean_temp: list[float] = []
    mean_precip: list[float] = []
    for month in MONTHS:
        if not temp_n[month] or not precip_lists[month]:
            raise ValueError(f"incomplete history: no data for month {month}")
        mean_temp.append(round(temp_sum[month] / temp_n[month], 1))
        mean_precip.append(round(sum(precip_lists[month]) / len(precip_lists[month]), 1))
    return mean_temp, mean_precip


def fetch_normals(
    lat: float,
    lon: float,
    start_year: int,
    end_year: int,
    *,
    retries: int = 4,
    timeout: float = 90.0,
) -> dict[str, list[float]]:
    """Fetch + aggregate Open-Meteo climate normals for one location.

    Returns ``{"temp_c": [12], "precip_mm": [12], "suitability": [12]}``. Retries
    with exponential backoff on Open-Meteo's transient statuses (429/5xx).
    """
    params = urllib.parse.urlencode(
        {
            "latitude": lat,
            "longitude": lon,
            "start_date": f"{start_year}-01-01",
            "end_date": f"{end_year}-12-31",
            "daily": "temperature_2m_mean,precipitation_sum",
            "timezone": "auto",
        }
    )
    url = f"{ARCHIVE_URL}?{params}"
    last_exc: Exception | None = None
    for attempt in range(retries):
        try:
            request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
            with urllib.request.urlopen(request, timeout=timeout) as response:  # noqa: S310
                payload = json.load(response)
            daily = payload["daily"]
            temp_c, precip_mm = monthly_normals(
                daily["time"], daily["temperature_2m_mean"], daily["precipitation_sum"]
            )
            suitability = [
                round(month_suitability(t, p), 4) for t, p in zip(temp_c, precip_mm, strict=True)
            ]
            return {"temp_c": temp_c, "precip_mm": precip_mm, "suitability": suitability}
        except (
            urllib.error.HTTPError,
            urllib.error.URLError,
            TimeoutError,
            KeyError,
            ValueError,
        ) as exc:
            last_exc = exc
            wait = 2 ** (attempt + 1)
            logger.warning(
                "Open-Meteo attempt %d/%d failed (%s); retrying in %ds",
                attempt + 1,
                retries,
                exc,
                wait,
            )
            time.sleep(wait)
    raise RuntimeError(f"Open-Meteo request failed after {retries} attempts: {last_exc}")


def fetch_all(
    destinations: list[dict[str, str]],
    *,
    start_year: int = DEFAULT_START_YEAR,
    end_year: int = DEFAULT_END_YEAR,
    sleep_s: float = DEFAULT_SLEEP_S,
) -> tuple[dict[str, dict[str, list[float]]], SeasonIngestSummary]:
    """Fetch seasonal profiles for every destination; failures are skipped."""
    summary = SeasonIngestSummary(total=len(destinations), period=f"{start_year}-{end_year}")
    profiles: dict[str, dict[str, list[float]]] = {}
    for i, dest in enumerate(destinations):
        city, country = dest["city"], dest["country"]
        try:
            lat, lon = float(dest["lat"]), float(dest["lon"])
        except (KeyError, ValueError) as exc:
            logger.warning("skipping %s, %s: bad coordinates (%s)", city, country, exc)
            summary.failed += 1
            summary.failures.append(f"{city}: bad coordinates")
            continue
        try:
            profile = fetch_normals(lat, lon, start_year, end_year)
        except RuntimeError as exc:
            logger.error("skipping %s, %s: %s", city, country, exc)
            summary.failed += 1
            summary.failures.append(f"{city}: {exc}")
            continue

        profiles[f"{city}|{country}"] = profile
        summary.fetched += 1
        peak = max(range(12), key=lambda m: profile["suitability"][m]) + 1
        logger.info(
            "%s, %s: peak month %d (suitability %.2f)",
            city,
            country,
            peak,
            max(profile["suitability"]),
        )
        if sleep_s and i < len(destinations) - 1:
            time.sleep(sleep_s)
    return profiles, summary


def _existing_profiles(path: Path) -> dict[str, dict[str, object]]:
    if not path.is_file():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}
    entries = payload.get("data", []) if isinstance(payload, dict) else []
    return {
        f"{e['city']}|{e['country']}": e
        for e in entries
        if isinstance(e, dict) and "city" in e and "country" in e
    }


def write_profiles(
    all_destinations: list[dict[str, str]],
    fresh: dict[str, dict[str, list[float]]],
    *,
    period: str,
    path: Path = SNAPSHOT_PATH,
) -> MergeReport:
    """Merge fresh profiles onto the committed snapshot and write it.

    A destination fetched this run is refreshed; one that failed keeps its
    last-good committed profile; one with neither is missing and omitted. Logs
    which cities fell in each bucket.
    """
    existing = _existing_profiles(path)
    report = MergeReport()
    data: list[dict[str, object]] = []

    for dest in all_destinations:
        city, country = dest["city"], dest["country"]
        key = f"{city}|{country}"
        if key in fresh:
            entry = {"city": city, "country": country, **fresh[key]}
            report.refreshed.append(city)
        elif key in existing:
            entry = existing[key]
            report.kept.append(city)
        else:
            report.missing.append(city)
            continue
        data.append(entry)

    payload = {
        "meta": {
            "title": "Per-city seasonal suitability (monthly)",
            "source": "Open-Meteo archive (ERA5 reanalysis)",
            "license": "CC BY 4.0",
            "attribution": "Weather data by Open-Meteo.com",
            "normal_period": period,
            "comfort_function": {
                "ideal_temp_c": IDEAL_TEMP_C,
                "temp_sigma_c": TEMP_SIGMA_C,
                "precip_zero_score_mm": PRECIP_ZERO_SCORE_MM,
                "temp_weight": TEMP_WEIGHT,
                "precip_weight": PRECIP_WEIGHT,
            },
            "fetched": date.today().isoformat(),
            "note": "entries not refreshed on the latest run retain earlier values",
            "generated_by": "scripts.ingest_season_normals",
        },
        "data": sorted(data, key=lambda e: (e["country"], e["city"])),
    }
    with path.open("w", encoding="utf-8") as fh:
        json.dump(payload, fh, ensure_ascii=False, indent=2)
        fh.write("\n")

    logger.info(
        "season profiles: %d refreshed, %d kept (last-good), %d missing",
        len(report.refreshed),
        len(report.kept),
        len(report.missing),
    )
    logger.info(
        "refreshed this run: %s", ", ".join(report.refreshed) if report.refreshed else "(none)"
    )
    if report.kept:
        logger.warning("kept last-good profiles: %s", ", ".join(report.kept))
    if report.missing:
        logger.warning("omitted (no fresh or prior data): %s", ", ".join(report.missing))
    return report


def _configure_logging() -> None:
    if not logging.getLogger().handlers:
        logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Ingest per-city seasonal suitability from Open-Meteo."
    )
    parser.add_argument(
        "--start", type=int, default=DEFAULT_START_YEAR, help="first year of normals"
    )
    parser.add_argument("--end", type=int, default=DEFAULT_END_YEAR, help="last year of normals")
    parser.add_argument(
        "--sleep", type=float, default=DEFAULT_SLEEP_S, help="seconds between cities"
    )
    parser.add_argument("--city", default=None, help="only ingest this city (merged in)")
    parser.add_argument("--dry-run", action="store_true", help="fetch and report without writing")
    args = parser.parse_args(argv)

    _configure_logging()

    try:
        all_destinations = read_destinations()
    except (FileNotFoundError, ValueError) as exc:
        logger.error("season ingestion aborted: %s", exc)
        return 1

    targets = all_destinations
    if args.city:
        targets = [d for d in all_destinations if d["city"].lower() == args.city.lower()]
        if not targets:
            logger.error("no destination named %r", args.city)
            return 1

    fresh, summary = fetch_all(
        targets, start_year=args.start, end_year=args.end, sleep_s=args.sleep
    )

    if not args.dry_run and fresh:
        write_profiles(all_destinations, fresh, period=summary.period)

    logger.info(
        "season ingestion complete: %d/%d fetched, %d failed (period %s)%s",
        summary.fetched,
        summary.total,
        summary.failed,
        summary.period,
        " [dry-run, no writes]" if args.dry_run else "",
    )
    return 0 if summary.failed == 0 else 1


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
