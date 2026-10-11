"""Enable or disable a candidate destination (soft on/off, no data loss).

The flip side of onboarding: instead of adding or pruning a city, this takes an
existing destination in or out of the candidate pool without deleting it. A
disabled city keeps all its rows (features, accommodation, POIs) but is skipped
by ScoutAgent's candidate load and the public listing, so it can be re-enabled
later. This is the no-admin-panel way to curate the live list.

Usage (from ``backend/``)::

    python -m scripts.set_destination_active "Naples" --disable          # take it out
    python -m scripts.set_destination_active "Naples" --enable           # put it back
    python -m scripts.set_destination_active "Split" --country Croatia --disable
    python -m scripts.set_destination_active --dry-run "Naples" --disable  # preview

Matching is on city name (case-insensitive); add ``--country`` to disambiguate
when more than one country has a city of that name. Exit is non-zero when any
name could not be matched unambiguously.
"""

from __future__ import annotations

import argparse
import logging

from db.models import Destination
from db.session import SessionLocal, get_engine
from sqlalchemy import func, select
from sqlalchemy.orm import Session

logger = logging.getLogger(__name__)


def _configure_logging() -> None:
    if not logging.getLogger().handlers:
        logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")


def set_active(
    session: Session,
    names: list[str],
    *,
    active: bool,
    country: str | None = None,
    dry_run: bool = False,
) -> tuple[list[str], list[str], list[str]]:
    """Set ``active`` on each named city. Returns (changed, not_found, ambiguous) labels."""
    changed: list[str] = []
    not_found: list[str] = []
    ambiguous: list[str] = []

    for name in names:
        stmt = select(Destination).where(func.lower(Destination.city) == name.strip().lower())
        if country:
            stmt = stmt.where(func.lower(Destination.country) == country.strip().lower())
        matches = list(session.scalars(stmt).all())

        if not matches:
            not_found.append(name)
            continue
        if len(matches) > 1 and not country:
            ambiguous.append(name)
            logger.warning(
                "%r is ambiguous across %s — pass --country to choose",
                name,
                ", ".join(sorted(m.country for m in matches)),
            )
            continue

        for dest in matches:
            dest.active = active
            changed.append(f"{dest.city}, {dest.country}")

    if changed and not dry_run:
        session.commit()
    else:
        session.rollback()
    return changed, not_found, ambiguous


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Enable or disable candidate destinations without deleting them."
    )
    parser.add_argument("names", nargs="+", help="city names to toggle")
    parser.add_argument("--country", default=None, help="disambiguate a city shared by countries")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--enable", action="store_true", help="put the city back in the pool")
    group.add_argument("--disable", action="store_true", help="take the city out of the pool")
    parser.add_argument(
        "--dry-run", action="store_true", help="report what would change without writing"
    )
    args = parser.parse_args(argv)

    _configure_logging()
    active = args.enable  # exactly one of enable/disable is set (mutually exclusive, required)

    session = SessionLocal(bind=get_engine())
    try:
        changed, not_found, ambiguous = set_active(
            session,
            args.names,
            active=active,
            country=args.country,
            dry_run=args.dry_run,
        )
    finally:
        session.close()

    verb = "enabled" if active else "disabled"
    suffix = " [dry-run, no writes]" if args.dry_run else ""
    for label in changed:
        logger.info("%s: %s%s", verb, label, suffix)
    if not_found:
        logger.warning("not found: %s", ", ".join(not_found))
    if ambiguous:
        logger.warning("ambiguous (needs --country): %s", ", ".join(ambiguous))

    return 0 if changed and not not_found and not ambiguous else 1


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
