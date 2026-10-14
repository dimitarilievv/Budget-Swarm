"""One-command migrate + seed orchestration (BS-104).

Applies the database migrations, then runs the **offline, idempotent** seed steps
that make a fresh BudgetSwarm database usable:

1. ``alembic upgrade head``                         — schema
2. ``ingest_destinations``                          — curated candidate cities
3. ``compute_destination_features --cost-source snapshot`` — Scout ranking features
4. ``ingest_fuel_prices --source snapshot``         — per-country fuel (car mode)

All four read only committed data under ``data/`` (no network) and are idempotent
(upserts), so the whole script is safe to re-run.

The Inside Airbnb accommodation import (``--with-accommodations``) is **opt-in**:
it downloads large third-party snapshots over the network and is slow, so it is
not part of the default one-command bring-up. Note the **real** decision engine
(``ENABLE_REAL_ENGINE=true``) needs accommodation data to produce a plan — its
``estimated`` accommodation prices are derived per city from the Airbnb price
statistics (``accommodation_price_stats``), so without this import the real engine
returns ``NO_PLAN_AVAILABLE`` (missing accommodation). The default stub engine
returns a canned plan regardless. Full per-step docs + flags live in
``backend/README.md``; engine modes are covered in the repo-root ``README.md``.

Run from ``backend/`` with ``DATABASE_URL`` set (inside the backend container this
is already the case)::

    python -m scripts.seed_all                      # migrate + offline core seed
    python -m scripts.seed_all --with-accommodations  # also import Inside Airbnb (network, slow)
    python -m scripts.seed_all --skip-migrate       # data only (schema already applied)
    python -m scripts.seed_all --dry-run            # print the plan, run nothing
"""

from __future__ import annotations

import argparse
import logging
import subprocess
import sys
import time
from dataclasses import dataclass

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class Step:
    """One orchestration step.

    ``required`` steps abort the run on failure (the DB would be unusable without
    them); best-effort steps only warn, so a transient data-source hiccup cannot
    block an otherwise-usable bring-up.
    """

    name: str
    argv: list[str]
    description: str
    required: bool = True


# alembic is invoked as a module via the current interpreter so the step does not
# depend on the ``alembic`` console script being on PATH.
_MIGRATE = Step(
    name="migrate",
    argv=[sys.executable, "-m", "alembic", "upgrade", "head"],
    description="apply database migrations (alembic upgrade head)",
)


def _py(module: str, *args: str) -> list[str]:
    return [sys.executable, "-m", module, *args]


def build_plan(*, skip_migrate: bool, with_accommodations: bool) -> list[Step]:
    """Return the ordered steps to run for the requested options."""
    plan: list[Step] = []
    if not skip_migrate:
        plan.append(_MIGRATE)
    plan.extend(
        [
            Step(
                name="destinations",
                argv=_py("scripts.ingest_destinations"),
                description="load curated destinations (offline CSV)",
            ),
            Step(
                name="features",
                argv=_py(
                    "scripts.compute_destination_features",
                    "--cost-source",
                    "snapshot",
                ),
                description="compute Scout ranking features (offline snapshots)",
            ),
            Step(
                name="accommodation-estimates",
                argv=_py("scripts.estimate_accommodation_prices"),
                description="model estimated accommodation prices for uncovered cities (offline)",
                # Best-effort: skips cleanly if the calibration model is absent/weak, and
                # never overwrites real Inside Airbnb stats.
                required=False,
            ),
            Step(
                name="fuel",
                argv=_py("scripts.ingest_fuel_prices", "--source", "snapshot"),
                description="load per-country fuel prices (offline snapshot)",
                # Car mode degrades to estimated fuel if this is unavailable.
                required=False,
            ),
        ]
    )
    if with_accommodations:
        # Network + slow; best-effort so a download failure never blocks the core
        # seed. See backend/README.md for the full pipeline and per-step flags.
        plan.extend(
            [
                Step(
                    name="accommodations:ingest",
                    argv=_py("scripts.ingest_accommodations"),
                    description="import Inside Airbnb raw listings (network)",
                    required=False,
                ),
                Step(
                    name="accommodations:clean",
                    argv=_py("scripts.clean_accommodations"),
                    description="validate + EUR-normalize raw listings",
                    required=False,
                ),
                Step(
                    name="accommodations:normalize",
                    argv=_py("scripts.normalize_accommodations"),
                    description="normalize into accommodations + price stats",
                    required=False,
                ),
            ]
        )
    return plan


def run_step(step: Step) -> bool:
    """Run one step; return True on success. Never raises for a step failure."""
    logger.info("→ %s: %s", step.name, step.description)
    started = time.monotonic()
    try:
        result = subprocess.run(step.argv, check=False)
    except OSError as exc:  # e.g. interpreter/module not found
        logger.error("  %s could not start: %s", step.name, exc)
        return False
    elapsed = time.monotonic() - started
    if result.returncode == 0:
        logger.info("  %s done in %.1fs", step.name, elapsed)
        return True
    logger.error("  %s failed (exit %d) after %.1fs", step.name, result.returncode, elapsed)
    return False


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Apply migrations and seed a fresh BudgetSwarm database (idempotent)."
    )
    parser.add_argument(
        "--skip-migrate", action="store_true", help="do not run 'alembic upgrade head'"
    )
    parser.add_argument(
        "--with-accommodations",
        action="store_true",
        help="also import Inside Airbnb accommodations (network, slow; best-effort)",
    )
    parser.add_argument("--dry-run", action="store_true", help="print the planned steps and exit")
    args = parser.parse_args(argv)

    if not logging.getLogger().handlers:
        logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")

    plan = build_plan(skip_migrate=args.skip_migrate, with_accommodations=args.with_accommodations)

    if args.dry_run:
        logger.info("seed plan (%d steps):", len(plan))
        for i, step in enumerate(plan, 1):
            tag = "required" if step.required else "best-effort"
            logger.info("  %d. %-22s [%s] %s", i, step.name, tag, step.description)
        logger.info("dry run - nothing executed")
        return 0

    failed_required: list[str] = []
    failed_optional: list[str] = []
    for step in plan:
        if run_step(step):
            continue
        if step.required:
            # A required step failed: stop now rather than leave a half-seeded DB
            # whose state is harder to reason about.
            logger.error("aborting: required step '%s' failed", step.name)
            failed_required.append(step.name)
            break
        failed_optional.append(step.name)
        logger.warning("continuing despite best-effort step '%s'", step.name)

    if failed_required:
        logger.error("seed INCOMPLETE - required step(s) failed: %s", ", ".join(failed_required))
        return 1
    if failed_optional:
        logger.warning(
            "seed complete with warnings - best-effort step(s) skipped: %s",
            ", ".join(failed_optional),
        )
    else:
        logger.info("seed complete - database is ready")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
