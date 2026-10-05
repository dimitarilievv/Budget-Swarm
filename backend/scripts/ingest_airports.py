"""Ingest the OurAirports open dataset into a slim IATA snapshot (real-engine wiring).

Air-mode pricing (Travelpayouts) needs an **IATA airport code** for each endpoint,
but the ``destinations`` table stores only city coordinates and the origin is a
free-text city. Rather than hand-authoring a city→airport map (which would violate
the "ingest from a real source" rule), this script downloads the **OurAirports**
dataset — a public-domain, community-maintained global airport database — and
writes the commercial airports with an IATA code to ``data/airports.json``.

At request time :class:`ai_engine.utils.airports.AirportIndex` loads that snapshot
and resolves any coordinate (origin or destination) to its nearest airport, so no
schema change or per-city curation is required and arbitrary user origins work.

Only ``large_airport`` and ``medium_airport`` rows that carry a 3-letter IATA code
**and have scheduled passenger service** are kept (small airfields, heliports, and
closed/cargo-only fields are not sensible commercial endpoints), giving a few thousand
rows — small enough to scan in-process. The ``scheduled_service`` filter matters: a
closed hub such as Istanbul Atatürk (``ISL``, no scheduled service since 2019) sits
closer to the city centre than the operating airports, so without it the nearest-airport
lookup would pick a code the flight API cannot price — every fare from/to that city then
falls back to the distance estimate. Requiring scheduled service resolves Istanbul to the
operating airports (``IST`` / ``SAW``) instead.

Attribution: OurAirports data is released into the public domain
(https://ourairports.com/data/). Source CSV:
https://davidmegginson.github.io/ourairports-data/airports.csv

Usage (from ``backend/``)::

    python -m scripts.ingest_airports              # download + write data/airports.json
    python -m scripts.ingest_airports --dry-run    # download + report, no write
    python -m scripts.ingest_airports --out other.json
"""

from __future__ import annotations

import argparse
import csv
import io
import json
import logging
import urllib.error
import urllib.request
from datetime import UTC, datetime
from pathlib import Path

logger = logging.getLogger(__name__)

# backend/scripts/ingest_airports.py -> repo root is two parents up.
REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_OUT = REPO_ROOT / "data" / "airports.json"

OURAIRPORTS_URL = "https://davidmegginson.github.io/ourairports-data/airports.csv"
USER_AGENT = "BudgetSwarm/1.0 (academic thesis project; airport ingest)"

# Airport types worth pricing a commercial flight to/from.
_KEEP_TYPES = {"large_airport", "medium_airport"}


def fetch_csv(url: str = OURAIRPORTS_URL, *, timeout: float = 60.0) -> str:
    """Download the OurAirports CSV text (raises RuntimeError on a network failure)."""
    try:
        request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
        with urllib.request.urlopen(request, timeout=timeout) as response:  # noqa: S310 (https)
            return response.read().decode("utf-8")
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise RuntimeError(f"OurAirports download failed: {exc}") from exc


def parse_airports(csv_text: str) -> list[dict[str, object]]:
    """Parse the CSV into slim ``{iata, name, lat, lon, type}`` records.

    Keeps only large/medium airports that carry a 3-letter IATA code, **have scheduled
    passenger service**, and have valid coordinates; everything else (heliports, closed or
    cargo-only fields such as Istanbul Atatürk/``ISL``, code-less strips) is dropped so the
    snapshot stays small and every row is a viable, priceable commercial endpoint.
    """
    reader = csv.DictReader(io.StringIO(csv_text))
    records: list[dict[str, object]] = []
    for row in reader:
        if row.get("type") not in _KEEP_TYPES:
            continue
        # Require scheduled passenger service so the nearest-airport lookup never returns a
        # closed/cargo-only field (e.g. ISL) that the flight API rejects — which would force
        # every fare for that city onto the distance estimate.
        if (row.get("scheduled_service") or "").strip().lower() != "yes":
            continue
        iata = (row.get("iata_code") or "").strip().upper()
        if len(iata) != 3 or not iata.isalpha():
            continue
        try:
            lat = float(row["latitude_deg"])
            lon = float(row["longitude_deg"])
        except (KeyError, ValueError, TypeError):
            continue
        if not (-90.0 <= lat <= 90.0 and -180.0 <= lon <= 180.0):
            continue
        records.append(
            {
                "iata": iata,
                "name": (row.get("name") or "").strip(),
                "lat": round(lat, 5),
                "lon": round(lon, 5),
                "type": row["type"],
            }
        )
    # Stable order (by IATA) so re-runs produce byte-identical snapshots when the
    # upstream data is unchanged.
    records.sort(key=lambda r: r["iata"])
    return records


def write_snapshot(records: list[dict[str, object]], out: Path = DEFAULT_OUT) -> None:
    """Write the airports snapshot as ``{source, generated_at, count, airports}``."""
    payload = {
        "source": "OurAirports (public domain)",
        "source_url": OURAIRPORTS_URL,
        "generated_at": datetime.now(UTC).isoformat(),
        "count": len(records),
        "airports": records,
    }
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def _configure_logging() -> None:
    if not logging.getLogger().handlers:
        logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Ingest OurAirports into data/airports.json.")
    parser.add_argument(
        "--out", type=Path, default=DEFAULT_OUT, help=f"output path (default: {DEFAULT_OUT})"
    )
    parser.add_argument("--url", default=OURAIRPORTS_URL, help="source CSV URL")
    parser.add_argument("--dry-run", action="store_true", help="download + report, no write")
    args = parser.parse_args(argv)

    _configure_logging()
    try:
        csv_text = fetch_csv(args.url)
    except RuntimeError as exc:
        logger.error("airport ingest aborted: %s", exc)
        return 1

    records = parse_airports(csv_text)
    if not records:
        logger.error("airport ingest: parsed 0 usable airports — aborting without writing")
        return 1

    if args.dry_run:
        logger.info(
            "dry-run: parsed %d commercial airports with IATA codes (no write)", len(records)
        )
        return 0

    write_snapshot(records, args.out)
    logger.info("airport ingest complete: wrote %d airports to %s", len(records), args.out)
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
