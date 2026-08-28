"""Tests for destination feature engineering (BS-031).

Two layers:

* **Unit** (no database) — normalization bounds, the constant-column edge case,
  the month-dependent season model, raw-CSV validation, and a spot-check that the
  shipped raw dataset yields scores in [0,1] with a populated cost_of_living.
* **Integration** (``integration`` marker, skipped without a live database) —
  load destinations, compute + store features against a freshly migrated schema,
  and assert the acceptance criteria: every destination has all feature scores in
  [0,1], cost_of_living is populated, season_score varies by month, and re-running
  is idempotent.
"""

from decimal import Decimal
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from config import get_settings
from db.models import Destination, DestinationFeature
from scripts.compute_destination_features import (
    CLIMATE_PROFILES,
    DEFAULT_RAW_CSV,
    FACTOR_COLUMNS,
    compute_and_store,
    compute_features,
    min_max_normalize,
    parse_raw_rows,
    read_raw_csv,
    season_score,
    window_months,
)
from scripts.ingest_destinations import DEFAULT_CSV as DESTINATIONS_CSV
from scripts.ingest_destinations import ingest
from scripts.wherenext import load_cost_index
from sqlalchemy import create_engine, func, select, text
from sqlalchemy.orm import Session

BACKEND_DIR = Path(__file__).resolve().parents[1]

_SCORE_FIELDS = (
    "cost_of_living",
    "culture_score",
    "nature_score",
    "nightlife_score",
    "food_score",
    "tourism_score",
    "season_score",
)

# WhereNext cost map from the committed snapshot — keeps the unit tests offline
# (no live API call) and deterministic.
COST_MAP = load_cost_index(source="snapshot").mapping


# --------------------------------------------------------------------------- #
# Unit — normalization
# --------------------------------------------------------------------------- #


def test_min_max_normalize_maps_to_unit_interval():
    out = min_max_normalize([10.0, 20.0, 30.0, 50.0])
    assert out[0] == 0.0  # min -> 0
    assert out[-1] == 1.0  # max -> 1
    assert all(0.0 <= v <= 1.0 for v in out)


def test_min_max_normalize_constant_column_is_neutral():
    assert min_max_normalize([7.0, 7.0, 7.0]) == [0.5, 0.5, 0.5]


def test_min_max_normalize_empty():
    assert min_max_normalize([]) == []


# --------------------------------------------------------------------------- #
# Unit — season model
# --------------------------------------------------------------------------- #


def test_window_months_wraps_around_year_end():
    assert window_months(11, 3) == (11, 12, 1)
    assert window_months(6, 1) == (6,)


@pytest.mark.parametrize("start,window", [(0, 1), (13, 1), (6, 0)])
def test_window_months_rejects_out_of_range(start, window):
    with pytest.raises(ValueError):
        window_months(start, window)


def test_season_score_varies_by_month():
    # A Mediterranean destination is far better in July than in January.
    july = season_score("mediterranean", [7])
    january = season_score("mediterranean", [1])
    assert july > january
    assert 0.0 <= january <= 1.0 and 0.0 <= july <= 1.0


def test_season_score_window_is_mean_of_months():
    profile = CLIMATE_PROFILES["continental"]
    expected = (profile[4] + profile[5] + profile[6]) / 3  # May, Jun, Jul
    assert season_score("continental", [5, 6, 7]) == pytest.approx(expected)


def test_all_climate_profiles_are_in_unit_interval():
    for climate, profile in CLIMATE_PROFILES.items():
        assert len(profile) == 12, climate
        assert all(0.0 <= v <= 1.0 for v in profile), climate


# --------------------------------------------------------------------------- #
# Unit — validation
# --------------------------------------------------------------------------- #


def _row(**overrides):
    base = {
        "city": "Sofia",
        "country": "Bulgaria",
        "country_code": "BG",
        "culture": "62",
        "nature": "78",
        "nightlife": "68",
        "food": "68",
        "tourism": "55",
        "climate": "continental",
    }
    base.update(overrides)
    return base


def test_parse_raw_rows_accepts_valid_row():
    rows, skipped = parse_raw_rows([_row()])
    assert skipped == []
    assert len(rows) == 1
    assert rows[0].climate == "continental"
    assert rows[0].country_code == "BG"
    assert rows[0].factors["culture"] == 62.0


@pytest.mark.parametrize(
    "bad",
    [
        _row(city=""),
        _row(country=""),
        _row(country_code=""),  # missing ISO code
        _row(country_code="BGR"),  # not alpha-2
        _row(culture="oops"),
        _row(nature="-5"),
        _row(climate="tropical"),
    ],
)
def test_parse_raw_rows_skips_malformed(bad):
    rows, skipped = parse_raw_rows([bad])
    assert rows == []
    assert len(skipped) == 1


def test_read_raw_csv_rejects_missing_column(tmp_path):
    bad = tmp_path / "bad.csv"
    bad.write_text("city,country,culture,climate\nX,Y,2,continental\n", encoding="utf-8")
    with pytest.raises(ValueError, match="missing required column"):
        read_raw_csv(bad)


def test_read_raw_csv_missing_file_raises():
    with pytest.raises(FileNotFoundError):
        read_raw_csv(Path("does-not-exist.csv"))


# --------------------------------------------------------------------------- #
# Unit — the shipped dataset
# --------------------------------------------------------------------------- #


def test_default_raw_csv_parses_cleanly():
    rows, skipped, total = read_raw_csv(DEFAULT_RAW_CSV)
    assert skipped == []
    assert len(rows) == total >= 20


def test_every_shipped_country_is_covered_by_wherenext():
    """The shipped destinations all resolve to a WhereNext country cost_index."""
    rows, _, _ = read_raw_csv(DEFAULT_RAW_CSV)
    missing = sorted({r.country_code for r in rows if r.country_code not in COST_MAP})
    assert missing == [], f"WhereNext snapshot is missing: {missing}"


def test_computed_scores_are_bounded_and_cost_populated():
    rows, _, _ = read_raw_csv(DEFAULT_RAW_CSV)
    features = compute_features(rows, months=[7], cost_by_country=COST_MAP)
    assert len(features) == len(rows)
    for key, record in features.items():
        for field in _SCORE_FIELDS:
            value = record[field]
            assert isinstance(value, Decimal), (key, field)
            assert Decimal("0") <= value <= Decimal("1"), (key, field, value)
        assert record["cost_of_living"] is not None


def test_cost_of_living_is_min_max_normalized_from_wherenext():
    # Cheapest destination in the set -> 0.0, most expensive -> 1.0.
    rows, _, _ = read_raw_csv(DEFAULT_RAW_CSV)
    features = compute_features(rows, months=[7], cost_by_country=COST_MAP)
    costs = [record["cost_of_living"] for record in features.values()]
    assert min(costs) == Decimal("0.000")
    assert max(costs) == Decimal("1.000")


def test_computed_season_score_differs_between_summer_and_winter():
    rows, _, _ = read_raw_csv(DEFAULT_RAW_CSV)
    summer = compute_features(rows, months=[7], cost_by_country=COST_MAP)
    winter = compute_features(rows, months=[1], cost_by_country=COST_MAP)
    # At least one destination's season_score moves between July and January.
    assert any(summer[key]["season_score"] != winter[key]["season_score"] for key in summer)


def test_missing_country_yields_null_cost_without_breaking_others():
    rows, _, _ = read_raw_csv(DEFAULT_RAW_CSV)
    # Drop one country from the cost map; that destination gets None, others stay bounded.
    dropped = rows[0].country_code
    partial = {k: v for k, v in COST_MAP.items() if k != dropped}
    features = compute_features(rows, months=[7], cost_by_country=partial)
    for (_, _country), record in features.items():
        cost = record["cost_of_living"]
        assert cost is None or Decimal("0") <= cost <= Decimal("1")
    assert features[(rows[0].city, rows[0].country)]["cost_of_living"] is None


def test_min_max_endpoints_present_in_each_factor():
    # Min-max across the set guarantees a 0.0 and a 1.0 in every factor column.
    rows, _, _ = read_raw_csv(DEFAULT_RAW_CSV)
    features = compute_features(rows, months=[7], cost_by_country=COST_MAP)
    for col in FACTOR_COLUMNS:
        column_values = [record[f"{col}_score"] for record in features.values()]
        assert min(column_values) == Decimal("0.000")
        assert max(column_values) == Decimal("1.000")


# --------------------------------------------------------------------------- #
# Integration — real compute against a migrated database
# --------------------------------------------------------------------------- #


def _db_reachable(url: str) -> bool:
    try:
        engine = create_engine(url, connect_args={"connect_timeout": 2})
        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))
        return True
    except Exception:
        return False


def _alembic_config() -> Config:
    cfg = Config(str(BACKEND_DIR / "alembic.ini"))
    cfg.set_main_option("script_location", str(BACKEND_DIR / "alembic"))
    return cfg


@pytest.fixture
def migrated_engine():
    url = get_settings().database_url
    if not _db_reachable(url):
        pytest.skip("no reachable database — integration test skipped")

    cfg = _alembic_config()
    command.downgrade(cfg, "base")
    command.upgrade(cfg, "head")

    engine = create_engine(url)
    try:
        yield engine
    finally:
        engine.dispose()
        command.downgrade(cfg, "base")


def _count(session: Session, model) -> int:
    return session.scalar(select(func.count()).select_from(model)) or 0


@pytest.mark.integration
def test_features_cover_every_destination_and_are_bounded(migrated_engine):
    """Every loaded destination gets feature scores in [0,1] with cost populated."""
    with Session(migrated_engine) as session:
        ingest(session, DESTINATIONS_CSV)
        session.commit()
        dest_count = _count(session, Destination)

        summary = compute_and_store(session, months=[7], cost_source="snapshot")
        session.commit()

        assert summary.matched == dest_count
        assert summary.unmatched == 0
        assert summary.cost_source == "wherenext:snapshot"
        assert summary.cost_missing == 0  # WhereNext covers every shipped country
        assert _count(session, DestinationFeature) == dest_count

        for feat in session.scalars(select(DestinationFeature)).all():
            for field in _SCORE_FIELDS:
                value = getattr(feat, field)
                assert value is not None, field
                assert Decimal("0") <= value <= Decimal("1"), (field, value)


@pytest.mark.integration
def test_recompute_is_idempotent_and_updates_season(migrated_engine):
    """Re-running never duplicates; a different month refreshes season_score in place."""
    with Session(migrated_engine) as session:
        ingest(session, DESTINATIONS_CSV)
        session.commit()
        dest_count = _count(session, Destination)

        compute_and_store(session, months=[1], cost_source="snapshot")  # January
        session.commit()
        after_first = _count(session, DestinationFeature)
        sample = session.scalars(
            select(DestinationFeature).join(Destination).where(Destination.city == "Athens")
        ).one()
        january_season = sample.season_score

        compute_and_store(session, months=[7], cost_source="snapshot")  # July
        session.commit()
        after_second = _count(session, DestinationFeature)
        session.refresh(sample)
        july_season = sample.season_score

    assert after_first == after_second == dest_count  # no duplicates
    assert july_season > january_season  # season refreshed in place


@pytest.mark.integration
def test_unmatched_rows_are_skipped_not_fatal(migrated_engine, tmp_path):
    """A raw row with no matching destination is skipped and reported, not fatal."""
    csv_path = tmp_path / "raw.csv"
    csv_path.write_text(
        "city,country,country_code,culture,nature,nightlife,food,tourism,climate\n"
        "Atlantis,Nowhere,ZZ,50,50,50,50,50,oceanic\n",
        encoding="utf-8",
    )
    with Session(migrated_engine) as session:
        summary = compute_and_store(session, csv_path, months=[7], cost_source="snapshot")
        session.commit()

    assert summary.valid == 1
    assert summary.matched == 0
    assert summary.unmatched == 1
