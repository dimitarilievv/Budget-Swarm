"""Purge dead refresh tokens from the database (auth housekeeping).

Refresh-token rows are never usable again once they are past ``expires_at`` or
carry a ``revoked_at`` (rotation on refresh revokes the old row; logout revokes
the presented one). ``get_active_by_hash`` already ignores such rows, so they
are dead weight — without a sweep the ``refresh_tokens`` table grows without
bound. This command deletes them.

It is idempotent and safe to run repeatedly; a run with nothing to purge is a
no-op. Driven on a schedule by the ofelia ``refresh-token-purge`` job (see
``docker-compose.yml``), but can be run by hand too::

    python -m scripts.purge_refresh_tokens            # delete expired + revoked rows
    python -m scripts.purge_refresh_tokens --dry-run  # count them without deleting
"""

from __future__ import annotations

import argparse
import logging
from datetime import UTC, datetime

from db.repositories import RefreshTokenRepository
from db.session import SessionLocal, get_engine

logger = logging.getLogger(__name__)


def _configure_logging() -> None:
    if not logging.getLogger().handlers:
        logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Delete expired or revoked refresh tokens from the database."
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="report how many rows would be purged without deleting them",
    )
    args = parser.parse_args(argv)

    _configure_logging()
    now = datetime.now(UTC)

    session = SessionLocal(bind=get_engine())
    try:
        # purge_dead only flushes, so a dry run can learn the count from the
        # flushed DELETE and then roll it back, writing nothing.
        removed = RefreshTokenRepository(session).purge_dead(now=now)
        if args.dry_run:
            session.rollback()
            logger.info("dry-run: %d dead refresh token(s) would be purged [no writes]", removed)
        else:
            session.commit()
            logger.info("purged %d dead refresh token(s)", removed)
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()

    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
