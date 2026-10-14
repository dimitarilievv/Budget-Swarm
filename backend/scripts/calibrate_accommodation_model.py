"""Calibrate the WhereNext → accommodation-price model from real data (BS-estimate).

**One-time / occasional developer step** (network-heavy). It fits a reproducible,
dependency-free linear model that maps a destination's WhereNext country cost signal
(``cost_index``) to a representative nightly Airbnb price in EUR, using the **real**
Inside Airbnb snapshot prices of the 11 covered cities as the ground truth. The fitted
coefficients, the leave-one-out error metrics, and the Q1/Q3 band ratios are written to
``data/accommodation_estimate_model.json``.

The committed model is then applied **offline** at seed time by
``scripts.estimate_accommodation_prices`` to produce explicitly-flagged ``estimated``
price statistics for the uncovered destinations — so that step needs no network and no
Airbnb import, as long as this calibration artifact exists.

This never invents prices: the anchors are real snapshot medians, the predictor is a
real cost-of-living index, and the output is a documented regression with its error
reported. Snapshot data is point-in-time, not live/bookable.

Usage (from ``backend/``)::

    python -m scripts.calibrate_accommodation_model                 # fit + write artifact
    python -m scripts.calibrate_accommodation_model --dry-run       # fit + report, no write
    python -m scripts.calibrate_accommodation_model --out path.json
"""

from __future__ import annotations

import argparse
import csv
import json
import logging
import sys
from datetime import UTC, datetime
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[2]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from scripts.clean_accommodations import clean_listings  # noqa: E402
from scripts.currency import load_eur_rates  # noqa: E402
from scripts.inside_airbnb import TARGET_CITIES, InsideAirbnbSource  # noqa: E402
from scripts.normalize_accommodations import price_stats  # noqa: E402

logger = logging.getLogger(__name__)

CURATED_CSV = _REPO_ROOT / "data" / "curated_destinations.csv"
WHERENEXT_JSON = _REPO_ROOT / "data" / "wherenext_cost_of_living.json"
DEFAULT_OUT = _REPO_ROOT / "data" / "accommodation_estimate_model.json"

# The predictor. cost_index is WhereNext's overall cost-of-living index (same signal
# already used for destination_features.cost_of_living), keeping the project consistent.
PREDICTOR = "cost_index"


def _city_country_codes() -> dict[str, str]:
    """city-slug -> ISO country code from the curated dataset."""
    out: dict[str, str] = {}
    with CURATED_CSV.open(encoding="utf-8", newline="") as fh:
        for row in csv.DictReader(fh):
            slug = row["city"].strip().lower().replace(" ", "-")
            out[slug] = row["country_code"].strip().upper()
    return out


def _wherenext_by_cc() -> dict[str, dict]:
    data = json.loads(WHERENEXT_JSON.read_text(encoding="utf-8"))
    return {r["country_code"]: r for r in data["data"]}


def _linfit(xs: list[float], ys: list[float]) -> tuple[float, float]:
    """Ordinary least squares slope/intercept for y = a + b*x (pure Python)."""
    n = len(xs)
    sx, sy = sum(xs), sum(ys)
    sxx = sum(x * x for x in xs)
    sxy = sum(x * y for x, y in zip(xs, ys, strict=True))
    denom = n * sxx - sx * sx
    if denom == 0:
        raise ValueError("predictor has no variance; cannot fit")
    b = (n * sxy - sx * sy) / denom
    a = (sy - b * sx) / n
    return a, b


def _r2(xs: list[float], ys: list[float], a: float, b: float) -> float:
    mean_y = sum(ys) / len(ys)
    ss_tot = sum((y - mean_y) ** 2 for y in ys)
    ss_res = sum((y - (a + b * x)) ** 2 for x, y in zip(xs, ys, strict=True))
    return 1.0 - (ss_res / ss_tot) if ss_tot else 0.0


def _loo_errors(xs: list[float], ys: list[float]) -> tuple[float, float]:
    """Leave-one-out MAPE (%) and MAE (EUR)."""
    abs_pct, abs_err = [], []
    for i in range(len(xs)):
        tx = xs[:i] + xs[i + 1 :]
        ty = ys[:i] + ys[i + 1 :]
        a, b = _linfit(tx, ty)
        pred = a + b * xs[i]
        abs_err.append(abs(pred - ys[i]))
        abs_pct.append(abs(pred - ys[i]) / ys[i] * 100.0 if ys[i] else 0.0)
    return sum(abs_pct) / len(abs_pct), sum(abs_err) / len(abs_err)


def collect_anchors(timeout: float = 120.0) -> list[dict]:
    """Download each covered city's snapshot and compute its real EUR price stats."""
    source = InsideAirbnbSource()
    rates = load_eur_rates(source="snapshot")
    ccs = _city_country_codes()
    wn = _wherenext_by_cc()
    anchors: list[dict] = []
    for city in TARGET_CITIES:
        cc = ccs.get(city)
        wn_row = wn.get(cc) if cc else None
        if wn_row is None:
            logger.warning("calibrate: no WhereNext row for %s (%s); skipping", city, cc)
            continue
        try:
            refs = source.discover(targets=(city,), timeout=timeout)
            ref = refs.get(city)
            if ref is None:
                logger.warning("calibrate: no snapshot in index for %s; skipping", city)
                continue
            path = source.download(ref, timeout=timeout)
            rows = (r for chunk in source.iter_chunks(path) for r in chunk)
            kept, report = clean_listings(
                rows,
                source=source.name,
                city=city,
                snapshot_label=ref.snapshot_label,
                currency=source.currency_for(city),
                rates=rates,
            )
        except Exception as exc:  # noqa: BLE001 - one city failing must not abort calibration
            logger.warning("calibrate: %s failed (%s); skipping", city, exc)
            continue
        stats = price_stats([k.price_per_night_eur for k in kept])
        if stats is None or stats.price_median <= 0:
            logger.warning("calibrate: %s produced no usable prices; skipping", city)
            continue
        anchors.append(
            {
                "city": city,
                "country_code": cc,
                "cost_index": float(wn_row[PREDICTOR]),
                "median_eur": stats.price_median,
                "q1_eur": stats.price_q1,
                "q3_eur": stats.price_q3,
                "sample_size": stats.sample_size,
                "snapshot_label": ref.snapshot_label,
            }
        )
        logger.info(
            "calibrate: %-14s cost_index=%s n=%d median=%.0f q1=%.0f q3=%.0f EUR",
            city,
            wn_row[PREDICTOR],
            stats.sample_size,
            stats.price_median,
            stats.price_q1,
            stats.price_q3,
        )
    return anchors


def fit_model(anchors: list[dict]) -> dict:
    """Fit the linear model + band ratios and compute error metrics."""
    if len(anchors) < 3:
        raise ValueError(f"need >= 3 anchors to fit, got {len(anchors)}")
    xs = [a["cost_index"] for a in anchors]
    ys = [a["median_eur"] for a in anchors]
    a_coef, b_coef = _linfit(xs, ys)
    q1_ratios = [a["q1_eur"] / a["median_eur"] for a in anchors if a["median_eur"]]
    q3_ratios = [a["q3_eur"] / a["median_eur"] for a in anchors if a["median_eur"]]
    loo_mape, loo_mae = _loo_errors(xs, ys)
    return {
        "method": "wherenext-cost-model",
        "model": "linear",
        "predictor": PREDICTOR,
        "predictor_source": "WhereNext cost-of-living index (CC BY 4.0)",
        "created_utc": datetime.now(UTC).isoformat(),
        "snapshot_dates": sorted({a["snapshot_label"] for a in anchors}),
        "coef": {"intercept_eur": round(a_coef, 4), "slope_eur_per_index": round(b_coef, 4)},
        "band": {
            "q1_ratio": round(sum(q1_ratios) / len(q1_ratios), 4),
            "q3_ratio": round(sum(q3_ratios) / len(q3_ratios), 4),
        },
        "predictor_range": {"min": min(xs), "max": max(xs)},
        "metrics": {
            "n_anchors": len(anchors),
            "r2_in_sample": round(_r2(xs, ys, a_coef, b_coef), 4),
            "loo_mape_pct": round(loo_mape, 2),
            "loo_mae_eur": round(loo_mae, 2),
        },
        "anchors": anchors,
        "notes": (
            "Nightly EUR median predicted from a country-level cost-of-living index; a "
            "proxy, not a city-level or live price. Snapshot-based, point-in-time. "
            "Apply only within predictor_range (interpolation)."
        ),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Calibrate the WhereNext accommodation model.")
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT, help="artifact path")
    parser.add_argument("--dry-run", action="store_true", help="fit + report, do not write")
    parser.add_argument("--timeout", type=float, default=120.0, help="per-download timeout (s)")
    args = parser.parse_args(argv)
    if not logging.getLogger().handlers:
        logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")

    anchors = collect_anchors(timeout=args.timeout)
    model = fit_model(anchors)
    m = model["metrics"]
    logger.info(
        "calibrated on %d cities: R2=%.3f  LOO MAPE=%.1f%%  LOO MAE=EUR%.0f  (median=%.3f*idx%+.3f)",
        m["n_anchors"],
        m["r2_in_sample"],
        m["loo_mape_pct"],
        m["loo_mae_eur"],
        model["coef"]["slope_eur_per_index"],
        model["coef"]["intercept_eur"],
    )
    if args.dry_run:
        logger.info("dry run - artifact not written")
        return 0
    args.out.write_text(json.dumps(model, indent=2) + "\n", encoding="utf-8")
    logger.info("wrote model artifact to %s", args.out)
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
