"""Seed-time WhereNext accommodation-price estimates for uncovered cities (BS-estimate).

Applies the committed calibration model (``data/accommodation_estimate_model.json``,
produced by ``scripts.calibrate_accommodation_model``) to the destinations **not**
covered by Inside Airbnb, writing explicitly-flagged ``estimated`` rows into
``accommodation_price_stats``. This runs **offline** — it needs no network and no
Airbnb import — so it fits the normal seed workflow; it only needs the committed model
artifact and the WhereNext snapshot (both in ``data/``) plus the loaded destinations.

Safety / honesty rules (never fabricate, never mislead):

* Only destinations **not** covered by Inside Airbnb are estimated — covered cities
  must use real snapshot data (``source = 'inside-airbnb'``).
* A real stats row (``sample_size > 0``) is **never overwritten** (guarded at the
  upsert) — estimates never clobber real data.
* A destination with no WhereNext cost anchor, or an anchor outside the model's fitted
  range (extrapolation), is **left without an estimate** and logged — no guess is made.
* The model is rejected if its leave-one-out error exceeds ``MAX_LOO_MAPE`` — a weak
  model produces nothing rather than misleading numbers.
* Rows are flagged ``source = 'wherenext-cost-model'`` with ``sample_size = 0`` and a
  ``wherenext-estimate-*`` ``snapshot_label``; no listings/names/photos/URLs are created.

Idempotent: upsert on ``destination_id`` (one row per destination), so re-running
refreshes estimates in place without duplicating. Snapshot-based "real" anchors are
point-in-time, not live/bookable; the estimate is a coarse, country-level proxy.

Usage (from ``backend/`` with ``DATABASE_URL`` set)::

    python -m scripts.estimate_accommodation_prices
    python -m scripts.estimate_accommodation_prices --dry-run
    python -m scripts.estimate_accommodation_prices --model path.json
"""

from __future__ import annotations

import argparse
import json
import logging
import math
import sys
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[2]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from db.models import AccommodationPriceStats, Destination  # noqa: E402
from db.session import SessionLocal, get_engine  # noqa: E402
from sqlalchemy import select  # noqa: E402
from sqlalchemy.dialects.postgresql import insert as pg_insert  # noqa: E402

from scripts.inside_airbnb import TARGET_CITIES  # noqa: E402

logger = logging.getLogger(__name__)

DEFAULT_MODEL = _REPO_ROOT / "data" / "accommodation_estimate_model.json"
WHERENEXT_JSON = _REPO_ROOT / "data" / "wherenext_cost_of_living.json"

# Reject a model whose leave-one-out error is too high to be a defensible estimate.
MAX_LOO_MAPE = 30.0

_COVERED_SLUGS = frozenset(TARGET_CITIES)


@dataclass(frozen=True)
class Model:
    intercept: float
    slope: float
    predictor: str
    q1_ratio: float
    q3_ratio: float
    range_min: float
    range_max: float
    loo_mape: float
    label: str

    def predict_median(self, cost_index: float) -> float:
        return self.intercept + self.slope * cost_index


def load_model(path: Path) -> Model:
    raw = json.loads(path.read_text(encoding="utf-8"))
    return Model(
        intercept=float(raw["coef"]["intercept_eur"]),
        slope=float(raw["coef"]["slope_eur_per_index"]),
        predictor=raw["predictor"],
        q1_ratio=float(raw["band"]["q1_ratio"]),
        q3_ratio=float(raw["band"]["q3_ratio"]),
        range_min=float(raw["predictor_range"]["min"]),
        range_max=float(raw["predictor_range"]["max"]),
        loo_mape=float(raw["metrics"]["loo_mape_pct"]),
        label="wherenext-estimate-"
        + (raw.get("snapshot_dates") or ["model"])[0].replace("inside-airbnb-", ""),
    )


def _slug(city: str) -> str:
    return city.strip().lower().replace(" ", "-")


def _wherenext_cost_by_cc(predictor: str) -> dict[str, float]:
    data = json.loads(WHERENEXT_JSON.read_text(encoding="utf-8"))
    return {r["country_code"]: float(r[predictor]) for r in data["data"] if predictor in r}


@dataclass
class EstimateRow:
    destination_id: int
    city: str
    cost_index: float
    price_q1: float
    price_median: float
    price_q3: float


def _band(model: Model, cost_index: float) -> tuple[float, float, float] | None:
    """Return (q1, median, q3) in EUR, or None if the prediction is not usable."""
    median = model.predict_median(cost_index)
    q1 = median * model.q1_ratio
    q3 = median * model.q3_ratio
    values = (q1, median, q3)
    if not all(math.isfinite(v) and v > 0 for v in values):
        return None
    q1, median, q3 = round(q1, 2), round(median, 2), round(q3, 2)
    if not (q1 <= median <= q3):  # guaranteed by ratios <1<; defensive
        return None
    return q1, median, q3


def plan_estimates(session, model: Model) -> tuple[list[EstimateRow], list[tuple[str, str]]]:
    """Compute estimate rows for uncovered destinations; return (rows, skipped[(city,why)])."""
    costs = _wherenext_cost_by_cc(model.predictor)
    rows: list[EstimateRow] = []
    skipped: list[tuple[str, str]] = []
    destinations = session.scalars(select(Destination)).all()
    for dest in destinations:
        if _slug(dest.city) in _COVERED_SLUGS:
            skipped.append((dest.city, "covered by Inside Airbnb (uses real data)"))
            continue
        cc = (dest.country_code or "").upper()
        cost_index = costs.get(cc)
        if cost_index is None:
            skipped.append((dest.city, f"no WhereNext anchor for country {cc!r}"))
            continue
        if not (model.range_min <= cost_index <= model.range_max):
            skipped.append((dest.city, f"cost_index {cost_index} outside fitted range"))
            continue
        band = _band(model, cost_index)
        if band is None:
            skipped.append((dest.city, "non-finite/invalid prediction"))
            continue
        q1, median, q3 = band
        rows.append(EstimateRow(dest.id, dest.city, cost_index, q1, median, q3))
    return rows, skipped


def _upsert_estimate(session, row: EstimateRow, label: str) -> None:
    """Upsert an estimate row, never overwriting a real (sample_size > 0) row."""
    values = {
        "destination_id": row.destination_id,
        "sample_size": 0,  # modeled, not a real sample
        "currency": "EUR",
        "price_min": row.price_q1,
        "price_q1": row.price_q1,
        "price_median": row.price_median,
        "price_q3": row.price_q3,
        "price_max": row.price_q3,
        "snapshot_label": label,
        "source": "wherenext-cost-model",
    }
    stmt = pg_insert(AccommodationPriceStats).values(values)
    stmt = stmt.on_conflict_do_update(
        index_elements=["destination_id"],
        set_={k: stmt.excluded[k] for k in values if k != "destination_id"}
        | {"computed_at": datetime.now(UTC)},
        # Guard: only replace an existing *estimate* — never clobber real stats.
        where=AccommodationPriceStats.sample_size == 0,
    )
    session.execute(stmt)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Write WhereNext-modeled accommodation estimates for uncovered cities."
    )
    parser.add_argument("--model", type=Path, default=DEFAULT_MODEL, help="calibration artifact")
    parser.add_argument("--dry-run", action="store_true", help="report only, no writes")
    args = parser.parse_args(argv)
    if not logging.getLogger().handlers:
        logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")

    if not args.model.is_file():
        logger.error(
            "no calibration model at %s — run scripts.calibrate_accommodation_model first",
            args.model,
        )
        return 1
    model = load_model(args.model)
    if model.loo_mape > MAX_LOO_MAPE:
        logger.error(
            "model LOO MAPE %.1f%% exceeds the %.1f%% limit — refusing to emit estimates",
            model.loo_mape,
            MAX_LOO_MAPE,
        )
        return 1

    session = SessionLocal(bind=get_engine())
    try:
        rows, skipped = plan_estimates(session, model)
        for city, why in skipped:
            logger.info("estimate: skip %s — %s", city, why)
        for row in rows:
            logger.info(
                "estimate: %-14s cost_index=%s -> q1=%.0f median=%.0f q3=%.0f EUR (estimated)",
                row.city,
                row.cost_index,
                row.price_q1,
                row.price_median,
                row.price_q3,
            )
        if args.dry_run:
            logger.info("dry run - %d estimate(s) computed, nothing written", len(rows))
            return 0
        for row in rows:
            _upsert_estimate(session, row, model.label)
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()

    logger.info(
        "wrote %d WhereNext accommodation estimate(s) (LOO MAPE %.1f%%, flagged estimated)",
        len(rows),
        model.loo_mape,
    )
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
