"""Unit tests for the WhereNext accommodation-estimate logic (offline, no DB).

Cover model loading, the Q1<=median<=Q3 band math, finiteness/positivity guards, the
covered-vs-uncovered + missing/out-of-range anchor handling in ``plan_estimates`` (via a
fake session over real WhereNext country codes), and the committed model's quality gate.
DB-write behaviour (upsert, never-overwrite-real, idempotency) is verified against a
real Postgres in the end-to-end run, not here.
"""

from __future__ import annotations

from db.models import Destination
from scripts.estimate_accommodation_prices import (
    DEFAULT_MODEL,
    MAX_LOO_MAPE,
    Model,
    _band,
    _slug,
    load_model,
    plan_estimates,
)

# A synthetic model (independent of the fitted coefficients) with the real fitted range.
_MODEL = Model(
    intercept=40.0,
    slope=1.3,
    predictor="cost_index",
    q1_ratio=0.73,
    q3_ratio=1.42,
    range_min=23.0,
    range_max=72.0,
    loo_mape=19.0,
    label="wherenext-estimate-test",
)


class _FakeScalars:
    def __init__(self, items):
        self._items = items

    def all(self):
        return self._items


class _FakeSession:
    def __init__(self, destinations):
        self._destinations = destinations

    def scalars(self, _stmt):
        return _FakeScalars(self._destinations)


# --- band math ---------------------------------------------------------------


def test_band_is_ordered_positive_and_finite():
    q1, median, q3 = _band(_MODEL, 32.0)  # Bulgaria-like index
    assert q1 <= median <= q3
    assert all(v > 0 for v in (q1, median, q3))
    assert median == round(40.0 + 1.3 * 32.0, 2)


def test_band_rejects_non_positive_prediction():
    bad = Model(-100.0, 0.0, "cost_index", 0.73, 1.42, 23.0, 72.0, 19.0, "x")
    assert _band(bad, 30.0) is None


# --- model artifact ----------------------------------------------------------


def test_committed_model_loads_and_passes_the_quality_gate():
    model = load_model(DEFAULT_MODEL)
    assert model.slope != 0.0 and model.range_min < model.range_max
    assert 0.0 < model.q1_ratio < 1.0 < model.q3_ratio
    # The committed calibration must be good enough to be used at all.
    assert model.loo_mape <= MAX_LOO_MAPE


# --- plan_estimates: coverage / anchor handling ------------------------------


def _dest(id_, city, cc):
    return Destination(id=id_, city=city, country_code=cc, latitude=42.0, longitude=23.0)


def test_uncovered_city_with_valid_anchor_gets_an_estimate():
    # Sofia (BG) has a WhereNext cost_index within the fitted range.
    rows, _ = plan_estimates(_FakeSession([_dest(1, "Sofia", "BG")]), _MODEL)
    assert len(rows) == 1
    r = rows[0]
    assert r.city == "Sofia" and r.price_q1 <= r.price_median <= r.price_q3 and r.price_median > 0


def test_covered_city_is_skipped_for_real_data():
    rows, skipped = plan_estimates(_FakeSession([_dest(1, "Vienna", "AT")]), _MODEL)
    assert rows == []
    assert any(city == "Vienna" and "covered" in why for city, why in skipped)


def test_missing_country_anchor_is_left_without_an_estimate():
    rows, skipped = plan_estimates(_FakeSession([_dest(1, "Atlantis", "ZZ")]), _MODEL)
    assert rows == []
    assert any(city == "Atlantis" and "no WhereNext anchor" in why for city, why in skipped)


def test_out_of_range_anchor_is_not_extrapolated():
    # Egypt (EG) cost_index (~19) is below the fitted range -> no extrapolation.
    rows, skipped = plan_estimates(_FakeSession([_dest(1, "Cairo", "EG")]), _MODEL)
    assert rows == []
    assert any(city == "Cairo" and "outside fitted range" in why for city, why in skipped)


def test_slug_normalisation():
    assert _slug("Novi Sad") == "novi-sad"
