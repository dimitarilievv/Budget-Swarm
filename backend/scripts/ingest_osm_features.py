"""Ingest destination factor signals from OpenStreetMap (BS-031).

Replaces hand-authored factor indices with **real POI counts from OpenStreetMap**
(the source named in ADR-007 and technical documentation §8/§19). For each
curated destination it queries the Overpass API for the number of matching POIs
within a fixed radius of the city centre, per factor:

* ``culture``   — museums, galleries, artworks, historic sites
* ``nature``    — parks, gardens, nature reserves, peaks, beaches, viewpoints
* ``nightlife`` — bars, pubs, nightclubs, biergartens
* ``food``      — restaurants, cafes, fast food
* ``tourism``   — hotels, hostels, guest houses, attractions, tourist info

Because the radius is fixed, a raw count is a POI **density** within a comparable
urban disc, which the feature-engineering step
(``scripts.compute_destination_features``) then min-max normalizes to [0,1].

Output artifacts (committed, so the pipeline is reproducible offline/in CI):

* ``data/osm_features_raw.json`` — the raw counts per destination + a ``meta``
  block (source, licence ODbL, radius, query date).
* ``data/destination_features_raw.csv`` — regenerated with the real counts; the
  ``climate`` column (Köppen-Geiger class, a factual lookup) is preserved.

OpenStreetMap data is © OpenStreetMap contributors, licensed under the ODbL.
No API key is required.

Usage (from ``backend/``)::

    python -m scripts.ingest_osm_features                 # all destinations
    python -m scripts.ingest_osm_features --city Sofia    # one city (testing)
    python -m scripts.ingest_osm_features --radius 10000  # custom radius (m)
    python -m scripts.ingest_osm_features --dry-run       # fetch + report, no writes
"""

from __future__ import annotations

import argparse
import csv
import json
import logging
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

logger = logging.getLogger(__name__)

# backend/scripts/ingest_osm_features.py -> repo root is two parents up.
REPO_ROOT = Path(__file__).resolve().parents[2]
DESTINATIONS_CSV = REPO_ROOT / "data" / "curated_destinations.csv"
RAW_FEATURES_CSV = REPO_ROOT / "data" / "destination_features_raw.csv"
SNAPSHOT_PATH = REPO_ROOT / "data" / "osm_features_raw.json"

OVERPASS_URL = "https://overpass-api.de/api/interpreter"
USER_AGENT = "BudgetSwarm/1.0 (academic thesis project; OSM Overpass ingestion)"
DEFAULT_RADIUS_M = 12000
DEFAULT_SLEEP_S = 1.5

# Factor -> Overpass tag filters (bracket contents). Each is expanded to a
# node+way lookup within the radius. Order defines the output column order.
CATEGORY_FILTERS: dict[str, list[str]] = {
    "culture": ['"tourism"~"^(museum|gallery|artwork)$"', '"historic"'],
    "nature": [
        '"leisure"~"^(park|garden|nature_reserve)$"',
        '"natural"~"^(peak|beach|wood)$"',
        '"tourism"="viewpoint"',
        '"boundary"="national_park"',
    ],
    "nightlife": ['"amenity"~"^(bar|pub|nightclub|biergarten)$"'],
    "food": ['"amenity"~"^(restaurant|cafe|fast_food)$"'],
    "tourism": ['"tourism"~"^(hotel|hostel|guest_house|apartment|attraction|information)$"'],
}
CATEGORIES = tuple(CATEGORY_FILTERS)


@dataclass
class OsmIngestSummary:
    """Outcome of an OSM ingestion run."""

    total: int = 0
    fetched: int = 0
    failed: int = 0
    radius_m: int = DEFAULT_RADIUS_M
    failures: list[str] = field(default_factory=list)


def read_destinations(path: Path = DESTINATIONS_CSV) -> list[dict[str, str]]:
    """Read curated destinations (needs city, country, country_code, lat, lon)."""
    if not path.is_file():
        raise FileNotFoundError(f"destinations CSV not found: {path}")
    with path.open(encoding="utf-8", newline="") as fh:
        rows = list(csv.DictReader(fh))
    if not rows:
        raise ValueError(f"{path.name} has no rows")
    return rows


def build_query(lat: float, lon: float, radius_m: int) -> str:
    """Build one Overpass query that emits a count per factor, in CATEGORIES order."""
    parts = ["[out:json][timeout:120];"]
    for category in CATEGORIES:
        union = "".join(
            f"node[{flt}](around:{radius_m},{lat},{lon});"
            f"way[{flt}](around:{radius_m},{lat},{lon});"
            for flt in CATEGORY_FILTERS[category]
        )
        parts.append(f"({union})->.{category};.{category} out count;")
    return "\n".join(parts)


def overpass_query(query: str, *, retries: int = 4, timeout: float = 180.0) -> list[int]:
    """POST an Overpass query, returning the per-factor counts in CATEGORIES order.

    Retries with exponential backoff on the transient statuses Overpass uses when
    busy (429 Too Many Requests, 504 Gateway Timeout) and on network errors.
    """
    data = urllib.parse.urlencode({"data": query}).encode()
    headers = {"User-Agent": USER_AGENT, "Accept": "application/json"}
    last_exc: Exception | None = None
    for attempt in range(retries):
        try:
            request = urllib.request.Request(OVERPASS_URL, data=data, headers=headers)
            with urllib.request.urlopen(request, timeout=timeout) as response:  # noqa: S310
                payload = json.load(response)
            counts = [
                int(element.get("tags", {}).get("total", 0))
                for element in payload.get("elements", [])
                if element.get("type") == "count"
            ]
            if len(counts) != len(CATEGORIES):
                raise ValueError(f"expected {len(CATEGORIES)} counts, got {len(counts)}: {counts}")
            return counts
        except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError, ValueError) as exc:
            last_exc = exc
            wait = 2 ** (attempt + 1)
            logger.warning(
                "Overpass attempt %d/%d failed (%s); retrying in %ds",
                attempt + 1,
                retries,
                exc,
                wait,
            )
            time.sleep(wait)
    raise RuntimeError(f"Overpass query failed after {retries} attempts: {last_exc}")


def fetch_all(
    destinations: list[dict[str, str]],
    *,
    radius_m: int = DEFAULT_RADIUS_M,
    sleep_s: float = DEFAULT_SLEEP_S,
) -> tuple[dict[str, dict[str, int]], OsmIngestSummary]:
    """Fetch per-factor POI counts for every destination.

    Returns ``(counts_by_key, summary)`` where ``counts_by_key`` maps
    ``"city|country"`` to ``{factor: count}``. A city that keeps failing is
    logged and skipped, never fatal.
    """
    summary = OsmIngestSummary(total=len(destinations), radius_m=radius_m)
    counts_by_key: dict[str, dict[str, int]] = {}
    for i, dest in enumerate(destinations):
        city, country = dest["city"], dest["country"]
        try:
            lat, lon = float(dest["lat"]), float(dest["lon"])
        except (KeyError, ValueError) as exc:
            logger.warning("skipping %s, %s: bad coordinates (%s)", city, country, exc)
            summary.failed += 1
            summary.failures.append(f"{city}: bad coordinates")
            continue

        query = build_query(lat, lon, radius_m)
        try:
            counts = overpass_query(query)
        except RuntimeError as exc:
            logger.error("skipping %s, %s: %s", city, country, exc)
            summary.failed += 1
            summary.failures.append(f"{city}: {exc}")
            continue

        counts_by_key[f"{city}|{country}"] = dict(zip(CATEGORIES, counts, strict=True))
        summary.fetched += 1
        logger.info(
            "%s, %s: %s",
            city,
            country,
            ", ".join(f"{c}={n}" for c, n in zip(CATEGORIES, counts, strict=True)),
        )
        if sleep_s and i < len(destinations) - 1:
            time.sleep(sleep_s)  # be polite to the shared Overpass instance
    return counts_by_key, summary


def write_snapshot(
    counts_by_key: dict[str, dict[str, int]], radius_m: int, path: Path = SNAPSHOT_PATH
) -> None:
    """Write the raw OSM counts + attribution meta as a committed snapshot."""
    payload = {
        "meta": {
            "title": "OpenStreetMap POI counts per destination",
            "source": "OpenStreetMap contributors via Overpass API",
            "license": "ODbL 1.0",
            "attribution": "© OpenStreetMap contributors",
            "radius_m": radius_m,
            "categories": {c: CATEGORY_FILTERS[c] for c in CATEGORIES},
            "fetched": date.today().isoformat(),
            "generated_by": "scripts.ingest_osm_features",
        },
        "data": [
            {"city": key.split("|")[0], "country": key.split("|")[1], **counts}
            for key, counts in counts_by_key.items()
        ],
    }
    with path.open("w", encoding="utf-8") as fh:
        json.dump(payload, fh, ensure_ascii=False, indent=2)
        fh.write("\n")
    logger.info("OSM snapshot written: %s (%d destinations)", path, len(counts_by_key))


def regenerate_features_csv(
    destinations: list[dict[str, str]],
    counts_by_key: dict[str, dict[str, int]],
    *,
    features_csv: Path = RAW_FEATURES_CSV,
) -> int:
    """Rewrite the raw features CSV with real OSM counts, preserving ``climate``.

    ``climate`` (Köppen-Geiger class) is a factual per-city lookup carried over
    from the existing CSV. A destination with no OSM counts (fetch failed) is
    omitted from the rewrite so we never emit a fabricated value.
    """
    climate_by_key = _existing_climate(features_csv)
    header = ["city", "country", "country_code", *CATEGORIES, "climate"]

    written = 0
    with features_csv.open("w", encoding="utf-8", newline="") as fh:
        writer = csv.writer(fh)
        writer.writerow(header)
        for dest in destinations:
            key = f"{dest['city']}|{dest['country']}"
            counts = counts_by_key.get(key)
            if counts is None:
                continue
            climate = climate_by_key.get(key, "")
            writer.writerow(
                [
                    dest["city"],
                    dest["country"],
                    dest["country_code"],
                    *(counts[c] for c in CATEGORIES),
                    climate,
                ]
            )
            written += 1
    logger.info("features CSV regenerated: %s (%d rows)", features_csv, written)
    return written


def _existing_climate(features_csv: Path) -> dict[str, str]:
    if not features_csv.is_file():
        return {}
    with features_csv.open(encoding="utf-8", newline="") as fh:
        return {
            f"{row['city']}|{row['country']}": row.get("climate", "") for row in csv.DictReader(fh)
        }


def _configure_logging() -> None:
    if not logging.getLogger().handlers:
        logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Ingest destination factor counts from OpenStreetMap."
    )
    parser.add_argument(
        "--radius", type=int, default=DEFAULT_RADIUS_M, help="search radius in metres"
    )
    parser.add_argument(
        "--sleep", type=float, default=DEFAULT_SLEEP_S, help="seconds between cities"
    )
    parser.add_argument("--city", default=None, help="only ingest this city (for testing)")
    parser.add_argument(
        "--dry-run", action="store_true", help="fetch and report without writing files"
    )
    args = parser.parse_args(argv)

    _configure_logging()

    try:
        destinations = read_destinations()
    except (FileNotFoundError, ValueError) as exc:
        logger.error("OSM ingestion aborted: %s", exc)
        return 1

    if args.city:
        destinations = [d for d in destinations if d["city"].lower() == args.city.lower()]
        if not destinations:
            logger.error("no destination named %r", args.city)
            return 1

    counts_by_key, summary = fetch_all(destinations, radius_m=args.radius, sleep_s=args.sleep)

    if not args.dry_run and counts_by_key:
        write_snapshot(counts_by_key, args.radius)
        # Only regenerate the CSV on a full run, so a --city test never truncates it.
        if not args.city:
            regenerate_features_csv(destinations, counts_by_key)

    logger.info(
        "OSM ingestion complete: %d/%d fetched, %d failed (radius %dm)%s",
        summary.fetched,
        summary.total,
        summary.failed,
        summary.radius_m,
        " [dry-run, no writes]" if args.dry_run else "",
    )
    return 0 if summary.failed == 0 else 1


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
