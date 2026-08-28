"""Ingest WhereNext cost-of-living data into the cache (BS-011 / BS-031).

Fetches the live WhereNext cost-of-living API (95 countries, CC BY 4.0) and
caches the raw payload in the ``api_cache`` table (provider ``wherenext``), so
the feature-engineering step (``scripts.compute_destination_features``) and the
Scout pre-filter read cached institutional data instead of calling the API on
every run (ADR-008; WhereNext allows 100 requests/hour). This is the
cost-of-living arm of the curated data pipeline:

    python -m scripts.ingest_destinations          # 1. destinations
    python -m scripts.ingest_cost_of_living         # 2. cost-of-living cache
    python -m scripts.compute_destination_features  # 3. normalized features

Usage (from ``backend/``)::

    python -m scripts.ingest_cost_of_living                 # live fetch + cache
    python -m scripts.ingest_cost_of_living --save-snapshot # also refresh the
                                                            #   committed snapshot
    python -m scripts.ingest_cost_of_living --source snapshot  # offline (no network)
    python -m scripts.ingest_cost_of_living --dry-run       # fetch + report, no writes
"""

from __future__ import annotations

import argparse
import json
import logging
from pathlib import Path

from db.session import SessionLocal, get_engine

from scripts import wherenext

logger = logging.getLogger(__name__)


def _configure_logging() -> None:
    if not logging.getLogger().handlers:
        logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")


def _save_snapshot(payload_source: str, timeout: float, path: Path) -> None:
    """Refresh the committed snapshot from a fresh live payload."""
    payload = wherenext.fetch_live(timeout)
    wherenext.parse_payload(payload)  # validate before persisting
    with path.open("w", encoding="utf-8") as fh:
        json.dump(payload, fh, ensure_ascii=False, indent=2)
        fh.write("\n")
    logger.info("WhereNext: snapshot refreshed at %s", path)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Ingest WhereNext cost-of-living data into cache.")
    parser.add_argument(
        "--source",
        choices=wherenext.VALID_SOURCES,
        default="live",
        help="where to read from: live (default), auto, cache, or snapshot",
    )
    parser.add_argument("--timeout", type=float, default=15.0, help="HTTP timeout in seconds")
    parser.add_argument(
        "--save-snapshot",
        action="store_true",
        help="also refresh data/wherenext_cost_of_living.json from a live fetch",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="fetch and report coverage without writing to the cache",
    )
    args = parser.parse_args(argv)

    _configure_logging()

    try:
        if args.save_snapshot:
            _save_snapshot(args.source, args.timeout, wherenext.SNAPSHOT_PATH)

        if args.dry_run:
            result = wherenext.load_cost_index(
                source=args.source if args.source != "cache" else "snapshot",
                timeout=args.timeout,
                write_cache=False,
            )
        else:
            session = SessionLocal(bind=get_engine())
            try:
                result = wherenext.load_cost_index(
                    session, source=args.source, timeout=args.timeout
                )
                session.commit()
            except Exception:
                session.rollback()
                raise
            finally:
                session.close()
    except (FileNotFoundError, ValueError, LookupError, OSError) as exc:
        logger.error("cost-of-living ingestion aborted: %s", exc)
        return 1

    version = result.meta.get("version", "?")
    licence = result.meta.get("license", "?")
    logger.info(
        "cost-of-living ingested: %d countries from %s (WhereNext %s, %s)%s",
        result.count,
        result.source,
        version,
        licence,
        "" if not args.dry_run else " [dry-run, no writes]",
    )
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
