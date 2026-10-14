"""Tests for the gated destination-onboarding command.

Two layers, matching the rest of ``scripts`` (see ``test_ingest_destinations``):

* **Unit** (no database) — the feasibility gate (:func:`evaluate_readiness` /
  :class:`DestinationReadiness`) and the CSV append/prune helpers.
* **Integration** (``integration`` marker, skipped without a live database) —
  the full ``onboard`` flow against a freshly migrated schema: a city with no
  accommodation is blocked and pruned out of both the DB and the CSV; a city
  that already has price stats passes the gate; ``--allow-incomplete`` waives it.

``geocode.best_match`` is monkeypatched throughout, so no network is used.
"""

import csv
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from config import get_settings
from db.models import AccommodationPriceStats, Destination
from db.repositories import AccommodationPriceStatsRepository, DestinationRepository
from scripts.geocode import GeocodeResult
from scripts.onboard_destination import (
    DestinationReadiness,
    evaluate_readiness,
    onboard,
)
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from scripts import add_destination, onboard_destination

BACKEND_DIR = Path(__file__).resolve().parents[1]


# --------------------------------------------------------------------------- #
# Unit — the gate verdict (no database)
# --------------------------------------------------------------------------- #


def _ready(**over) -> DestinationReadiness:
    base = dict(
        city="Lisbon",
        country="Portugal",
        destination_id=1,
        accommodation_source="price_stats",
        has_features=True,
        poi_count=12,
    )
    base.update(over)
    return evaluate_readiness(**base)


def test_feasible_requires_accommodation():
    assert _ready(accommodation_source="price_stats").feasible is True
    assert _ready(accommodation_source="listings").feasible is True
    assert _ready(accommodation_source=None).feasible is False


def test_missing_enrichment_lists_only_nonfatal_gaps():
    full = _ready()
    assert full.missing_enrichment == []
    gappy = _ready(has_features=False, poi_count=0)
    assert gappy.missing_enrichment == ["feature scores", "POIs"]
    # A city can be feasible (has stay) yet still enrichment-pending.
    assert gappy.feasible is True


def test_remediation_points_at_the_right_commands():
    blocked = _ready(accommodation_source=None, has_features=False, poi_count=0)
    steps = " ".join(blocked.remediation)
    assert "ingest_accommodations" in steps
    assert "normalize_accommodations" in steps
    assert "compute_destination_features" in steps
    assert "ingest_osm_features" in steps
    # A fully-ready destination needs nothing.
    assert _ready().remediation == []


# --------------------------------------------------------------------------- #
# Unit — CSV helpers (no database)
# --------------------------------------------------------------------------- #


def _seed_csv(path: Path, *cities: tuple[str, str]) -> None:
    with path.open("w", encoding="utf-8", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=add_destination.COLUMNS)
        writer.writeheader()
        for city, country in cities:
            writer.writerow(
                {"city": city, "country": country, "country_code": "XX", "lat": "1.0", "lon": "2.0"}
            )


def test_remove_csv_rows_drops_only_the_named_keys(tmp_path):
    path = tmp_path / "dest.csv"
    _seed_csv(path, ("Sofia", "Bulgaria"), ("Faro", "Portugal"), ("Porto", "Portugal"))
    onboard_destination._remove_csv_rows(path, {("Faro", "Portugal")})
    rows = list(csv.DictReader(path.open(encoding="utf-8")))
    assert [r["city"] for r in rows] == ["Sofia", "Porto"]


def test_remove_csv_rows_is_a_noop_when_nothing_matches(tmp_path):
    path = tmp_path / "dest.csv"
    _seed_csv(path, ("Sofia", "Bulgaria"))
    onboard_destination._remove_csv_rows(path, {("Nowhere", "Nowhereland")})
    rows = list(csv.DictReader(path.open(encoding="utf-8")))
    assert [r["city"] for r in rows] == ["Sofia"]


# --------------------------------------------------------------------------- #
# Integration — the full onboard flow (needs a migrated database)
# --------------------------------------------------------------------------- #


def _match(city, country, code="XX", lat=1.0, lon=2.0) -> GeocodeResult:
    return GeocodeResult(city, country, code, lat, lon)


def _db_reachable(url: str) -> bool:
    try:
        engine = create_engine(url, connect_args={"connect_timeout": 2})
        with engine.connect():
            return True
    except Exception:
        return False
    finally:
        engine.dispose()


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
    command.upgrade(cfg, "head")
    engine = create_engine(url)
    try:
        yield engine
    finally:
        engine.dispose()


def _patch_geocode(monkeypatch, match: GeocodeResult) -> None:
    monkeypatch.setattr(add_destination.geocode, "best_match", lambda name: match)


@pytest.mark.integration
def test_onboard_blocks_and_prunes_a_city_with_no_accommodation(
    migrated_engine, tmp_path, monkeypatch
):
    csv_path = tmp_path / "dest.csv"
    _seed_csv(csv_path)  # header only
    _patch_geocode(monkeypatch, _match("Faro", "Portugal"))

    with Session(migrated_engine) as session:
        report = onboard(session, ["Faro"], csv_path=csv_path)

    assert [f"{r.city}, {r.country}" for r in report.blocked] == ["Faro, Portugal"]
    assert report.ready == []
    assert report.pruned == ["Faro, Portugal"]
    # Rolled back out of the DB...
    with Session(migrated_engine) as session:
        assert (
            session.scalars(select(Destination).where(Destination.city == "Faro")).first() is None
        )
    # ...and out of the seed CSV.
    assert list(csv.DictReader(csv_path.open(encoding="utf-8"))) == []


@pytest.mark.integration
def test_onboard_passes_a_destination_that_has_price_stats(migrated_engine, tmp_path, monkeypatch):
    csv_path = tmp_path / "dest.csv"
    _seed_csv(csv_path)
    # Pre-create the destination with price stats, so onboard verifies an existing city.
    # Idempotent so repeated runs against a persistent DB don't hit the unique constraint.
    with Session(migrated_engine) as session:
        dest = DestinationRepository(session).get_by_city_country("Lisbon", "Portugal")
        if dest is None:
            dest = Destination(
                city="Lisbon",
                country="Portugal",
                country_code="PT",
                latitude=38.72,
                longitude=-9.14,
            )
            session.add(dest)
            session.flush()
        if AccommodationPriceStatsRepository(session).get_for_destination(dest.id) is None:
            session.add(AccommodationPriceStats(destination_id=dest.id, sample_size=40))
        session.commit()

    _patch_geocode(monkeypatch, _match("Lisbon", "Portugal", "PT", 38.72, -9.14))
    with Session(migrated_engine) as session:
        report = onboard(session, ["Lisbon"], csv_path=csv_path)

    assert [f"{r.city}, {r.country}" for r in report.ready] == ["Lisbon, Portugal"]
    assert report.blocked == []
    assert report.committed is True
    assert report.ready[0].accommodation_source == "price_stats"


@pytest.mark.integration
def test_allow_incomplete_waives_the_gate(migrated_engine, tmp_path, monkeypatch):
    csv_path = tmp_path / "dest.csv"
    _seed_csv(csv_path)
    _patch_geocode(monkeypatch, _match("Evora", "Portugal"))

    with Session(migrated_engine) as session:
        report = onboard(session, ["Evora"], csv_path=csv_path, require_accommodation=False)

    assert [f"{r.city}, {r.country}" for r in report.ready] == ["Evora, Portugal"]
    assert report.blocked == []
    assert report.pruned == []
    # Kept in the DB because the gate was waived.
    with Session(migrated_engine) as session:
        kept = session.scalars(select(Destination).where(Destination.city == "Evora")).first()
        assert kept is not None


@pytest.mark.integration
def test_dry_run_writes_nothing(migrated_engine, tmp_path, monkeypatch):
    csv_path = tmp_path / "dest.csv"
    _seed_csv(csv_path)
    _patch_geocode(monkeypatch, _match("Braga", "Portugal"))

    with Session(migrated_engine) as session:
        report = onboard(session, ["Braga"], csv_path=csv_path, dry_run=True)

    assert report.committed is False
    # Brand-new city has no data yet, so a dry run reports it as blocked...
    assert [f"{r.city}, {r.country}" for r in report.blocked] == ["Braga, Portugal"]
    # ...but nothing was written to the DB or the CSV.
    with Session(migrated_engine) as session:
        assert (
            session.scalars(select(Destination).where(Destination.city == "Braga")).first() is None
        )
    assert list(csv.DictReader(csv_path.open(encoding="utf-8"))) == []
