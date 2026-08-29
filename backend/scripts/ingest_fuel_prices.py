"""Idempotent ingestion of per-country fuel prices into ``fuel_prices`` (BS-032).

Resolves consumer fuel prices (Euro-95 + diesel, EUR/L, tax-incl.) via
``scripts.fuel_prices`` — EU-27 from the EU Weekly Oil Bulletin export, non-EU
neighbours (incl. North Macedonia) from the fuel-prices.eu ``/live/`` feed, plus
a synthetic ``EU`` average row — and upserts them into the ``fuel_prices`` table.

The load is:

* **Idempotent** — each row is upserted on its ``country_code`` primary key using
  PostgreSQL ``INSERT ... ON CONFLICT DO UPDATE``. Re-running never duplicates or
  errors; it refreshes ``petrol_e95``/``diesel``/``source``/``price_date`` in
  place and bumps ``updated_at``. This is the BS-032 "refresh routine".
* **Cache-not-live** — the app only ever reads ``fuel_prices`` from the DB
  (ADR-008). This script is the sole caller of the fuel-prices.eu endpoints; run
  it on a schedule (weekly, after the Thursday bulletin) to refresh.

Attribution: fuel-prices.eu — data from the European Commission Weekly Oil
Bulletin (CC BY 4.0).

Usage (from ``backend/``)::

    python -m scripts.ingest_fuel_prices                 # live fetch + upsert
    python -m scripts.ingest_fuel_prices --source snapshot  # offline (committed data/)
    python -m scripts.ingest_fuel_prices --dry-run       # resolve + report, no writes
    python -m scripts.ingest_fuel_prices --save-snapshot # also refresh data/ snapshots
"""

from __future__ import annotations

import argparse
import json
import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime

from db.models import FuelPrice
from db.session import SessionLocal, get_engine
from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from scripts import fuel_prices as fp

logger = logging.getLogger(__name__)


@dataclass
class IngestSummary:
    """Outcome of a fuel-price ingestion run, for logging and tests."""

    resolved: int = 0  # rows resolved from the sources
    inserted: int = 0  # new fuel_prices rows created
    updated: int = 0  # existing rows refreshed
    provider_sources: dict[str, str] = field(default_factory=dict)  # provider -> live|snapshot
    provenance: dict[str, str] = field(default_factory=dict)


def upsert_fuel_prices(session: Session, records: list[fp.FuelRecord]) -> None:
    """Upsert records into ``fuel_prices`` on the ``country_code`` primary key.

    ``ON CONFLICT DO UPDATE`` refreshes the mutable columns in place, so a re-run
    is a clean idempotent refresh rather than a duplicate insert.
    """
    if not records:
        return
    values = [
        {
            "country_code": r.country_code,
            "petrol_e95": r.petrol_e95,
            "diesel": r.diesel,
            "source": r.source,
            "price_date": r.price_date,
        }
        for r in records
    ]
    stmt = pg_insert(FuelPrice).values(values)
    stmt = stmt.on_conflict_do_update(
        index_elements=["country_code"],
        set_={
            "petrol_e95": stmt.excluded.petrol_e95,
            "diesel": stmt.excluded.diesel,
            "source": stmt.excluded.source,
            "price_date": stmt.excluded.price_date,
            "updated_at": datetime.now(UTC),
        },
    )
    session.execute(stmt)


def _count(session: Session) -> int:
    return session.scalar(select(func.count()).select_from(FuelPrice)) or 0


def ingest(
    session: Session | None,
    *,
    source: str = "auto",
    timeout: float = 20.0,
    dry_run: bool = False,
) -> tuple[IngestSummary, list[fp.FuelRecord]]:
    """Resolve fuel prices and (unless ``dry_run``) upsert them through ``session``.

    The caller owns the transaction: this flushes but does not commit, so tests
    roll back and the CLI commits once at the end.
    """
    resolved = fp.resolve_fuel_records(source=source, timeout=timeout)

    summary = IngestSummary(
        resolved=len(resolved.records),
        provider_sources=resolved.provider_sources,
        provenance=resolved.provenance,
    )
    if dry_run:
        return summary, resolved.records

    if session is None:
        raise ValueError("a database session is required unless dry_run=True")

    before = _count(session)
    upsert_fuel_prices(session, resolved.records)
    session.flush()
    after = _count(session)

    summary.inserted = after - before
    summary.updated = summary.resolved - summary.inserted
    return summary, resolved.records


def _save_snapshots(timeout: float) -> None:
    """Refresh the committed data/ snapshots from a fresh live fetch."""
    bulletin_text = fp.fetch_bulletin_live(timeout)
    fp.parse_bulletin_export(bulletin_text)  # validate before persisting
    fp.BULLETIN_SNAPSHOT.write_text(bulletin_text, encoding="utf-8")
    logger.info("fuel: bulletin snapshot refreshed at %s", fp.BULLETIN_SNAPSHOT)

    live_payload = fp.fetch_live_summary(timeout)
    fp.parse_live_summary(live_payload)  # validate before persisting
    with fp.LIVE_SNAPSHOT.open("w", encoding="utf-8") as fh:
        json.dump(live_payload, fh, ensure_ascii=False, indent=2)
        fh.write("\n")
    logger.info("fuel: live snapshot refreshed at %s", fp.LIVE_SNAPSHOT)


def _configure_logging() -> None:
    if not logging.getLogger().handlers:
        logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Ingest per-country fuel prices into the database."
    )
    parser.add_argument(
        "--source",
        choices=fp.VALID_SOURCES,
        default="auto",
        help="where to read from: auto (live then snapshot, default), live, or snapshot",
    )
    parser.add_argument("--timeout", type=float, default=20.0, help="HTTP timeout in seconds")
    parser.add_argument(
        "--save-snapshot",
        action="store_true",
        help="also refresh the committed data/ snapshots from a live fetch",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="resolve and report coverage without writing to the database",
    )
    args = parser.parse_args(argv)

    _configure_logging()

    try:
        if args.save_snapshot:
            _save_snapshots(args.timeout)

        if args.dry_run:
            summary, records = ingest(None, source=args.source, timeout=args.timeout, dry_run=True)
        else:
            session = SessionLocal(bind=get_engine())
            try:
                summary, records = ingest(session, source=args.source, timeout=args.timeout)
                session.commit()
            except Exception:
                session.rollback()
                raise
            finally:
                session.close()
    except (FileNotFoundError, ValueError, OSError) as exc:
        logger.error("fuel ingestion aborted: %s", exc)
        return 1

    # Provider-sourced rows carry a "name:loaded_from" provenance (a colon);
    # fallback/estimated rows (e.g. "eu-average") do not.
    real = sum(1 for label in summary.provenance.values() if ":" in label)
    estimated = summary.resolved - real
    sources = ", ".join(f"{name}={how}" for name, how in summary.provider_sources.items()) or "none"
    if args.dry_run:
        logger.info(
            "dry-run: %d row(s) resolved (%d real, %d fallback) from %s [no writes]",
            summary.resolved,
            real,
            estimated,
            sources,
        )
        for r in records:
            logger.info(
                "  %-2s petrol=%s diesel=%s  %s  %s",
                r.country_code,
                f"{r.petrol_e95:.3f}" if r.petrol_e95 is not None else "  -  ",
                f"{r.diesel:.3f}" if r.diesel is not None else "  -  ",
                r.price_date.isoformat(),
                r.source,
            )
    else:
        logger.info(
            "fuel ingest complete: %d resolved (%d real, %d fallback) - %d inserted, %d updated (%s)",
            summary.resolved,
            real,
            estimated,
            summary.inserted,
            summary.updated,
            sources,
        )
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
