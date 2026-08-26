"""Integration tests: ORM models + repository layer (BS-028).

Exercise repository CRUD on every MVP table against a live database after the
baseline migration (BS-018) has been applied. A module-scoped engine migrates
the schema once; each test runs inside its own transaction that is rolled back
afterwards, so tests stay isolated and fast without re-migrating each time.

Marked ``integration`` and skipped automatically when no database is reachable,
so they never block the fast unit-test path. In CI the backend job provisions a
Postgres service, so these run for real.
"""

from datetime import date, time
from decimal import Decimal
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from config import get_settings
from db.models import (
    Accommodation,
    ApiCache,
    Attraction,
    Destination,
    DestinationFeature,
    FuelPrice,
    TransportOption,
    Trip,
    TripActivity,
    User,
    WeatherForecast,
)
from db.repositories import (
    AccommodationRepository,
    ApiCacheRepository,
    AttractionRepository,
    DestinationFeatureRepository,
    DestinationRepository,
    FuelPriceRepository,
    TransportOptionRepository,
    TripActivityRepository,
    TripRepository,
    UserRepository,
    WeatherForecastRepository,
)
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

BACKEND_DIR = Path(__file__).resolve().parents[1]


def _db_reachable(url: str) -> bool:
    from sqlalchemy import text

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


@pytest.fixture(scope="module")
def engine():
    """Migrate the schema once for the module; tear it back down at the end."""
    url = get_settings().database_url
    if not _db_reachable(url):
        pytest.skip("no reachable database — integration test skipped")

    cfg = _alembic_config()
    command.downgrade(cfg, "base")
    command.upgrade(cfg, "head")

    eng = create_engine(url)
    try:
        yield eng
    finally:
        eng.dispose()
        command.downgrade(cfg, "base")


@pytest.fixture
def session(engine):
    """Yield a Session wrapped in a transaction rolled back after each test."""
    conn = engine.connect()
    trans = conn.begin()
    sess = Session(bind=conn, expire_on_commit=False)
    try:
        yield sess
    finally:
        sess.close()
        trans.rollback()
        conn.close()


def _new_destination(session: Session, city: str = "Ljubljana") -> Destination:
    dest = DestinationRepository(session).add(
        Destination(city=city, country="Slovenia", latitude=46.0569, longitude=14.5058)
    )
    return dest


@pytest.mark.integration
def test_destination_crud(session):
    repo = DestinationRepository(session)
    dest = repo.add(
        Destination(
            city="Lisbon",
            country="Portugal",
            country_code="PT",
            latitude=38.7223,
            longitude=-9.1393,
        )
    )
    assert dest.id is not None
    assert dest.created_at is not None  # server default filled

    assert repo.get(dest.id).city == "Lisbon"
    assert repo.get_by_city_country("Lisbon", "Portugal").id == dest.id

    dest.country_code = "PL"
    session.flush()
    assert repo.get(dest.id).country_code == "PL"

    assert repo.count() == 1
    repo.delete(dest)
    assert repo.get(dest.id) is None


@pytest.mark.integration
def test_destination_feature_crud(session):
    dest = _new_destination(session)
    repo = DestinationFeatureRepository(session)
    feat = repo.add(DestinationFeature(destination_id=dest.id, culture_score=Decimal("0.850")))
    assert repo.get(dest.id).culture_score == Decimal("0.850")

    feat.food_score = Decimal("0.900")
    session.flush()
    assert repo.get(dest.id).food_score == Decimal("0.900")

    repo.delete(feat)
    assert repo.get(dest.id) is None


@pytest.mark.integration
def test_fuel_price_crud(session):
    repo = FuelPriceRepository(session)
    fp = repo.add(
        FuelPrice(country_code="DE", petrol_e95=Decimal("1.789"), price_date=date(2026, 8, 24))
    )
    assert repo.get_by_country("DE").petrol_e95 == Decimal("1.789")
    assert repo.get("DE").source == "EU Weekly Oil Bulletin"  # server default

    fp.diesel = Decimal("1.699")
    session.flush()
    assert repo.get("DE").diesel == Decimal("1.699")

    repo.delete(fp)
    assert repo.get("DE") is None


@pytest.mark.integration
def test_accommodation_crud(session):
    dest = _new_destination(session)
    repo = AccommodationRepository(session)
    acc = repo.add(
        Accommodation(destination_id=dest.id, name="Studio", price_per_night=Decimal("82.50"))
    )
    assert acc.quality_flag == "real"  # server default
    assert [a.id for a in repo.list_by_destination(dest.id)] == [acc.id]

    acc.rating = Decimal("4.70")
    session.flush()
    assert repo.get(acc.id).rating == Decimal("4.70")

    repo.delete(acc)
    assert repo.list_by_destination(dest.id) == []


@pytest.mark.integration
def test_attraction_crud(session):
    dest = _new_destination(session)
    repo = AttractionRepository(session)
    attr = repo.add(Attraction(destination_id=dest.id, name="Old Town", indoor_outdoor="outdoor"))
    assert [a.id for a in repo.list_by_destination(dest.id)] == [attr.id]
    assert repo.list_by_destination(dest.id, indoor_outdoor="indoor") == []

    attr.category = "landmark"
    session.flush()
    assert repo.get(attr.id).category == "landmark"

    repo.delete(attr)
    assert repo.get(attr.id) is None


@pytest.mark.integration
def test_transport_option_crud(session):
    dest = _new_destination(session)
    repo = TransportOptionRepository(session)
    opt = repo.add(
        TransportOption(
            origin_label="Skopje",
            destination_id=dest.id,
            mode="bus",
            cost=Decimal("45.00"),
            route_summary={"changes": 1},
        )
    )
    assert opt.quality_flag == "real"  # server default
    found = repo.find("Skopje", dest.id, "bus")
    assert found.id == opt.id
    assert found.route_summary == {"changes": 1}  # JSONB round-trip

    opt.cost = Decimal("40.00")
    session.flush()
    assert repo.get(opt.id).cost == Decimal("40.00")

    repo.delete(opt)
    assert repo.find("Skopje", dest.id, "bus") is None


@pytest.mark.integration
def test_weather_forecast_crud(session):
    dest = _new_destination(session)
    repo = WeatherForecastRepository(session)
    wf = repo.add(WeatherForecast(destination_id=dest.id, forecast_date=date(2026, 7, 1)))
    assert wf.weather_conflict is False  # server default
    assert repo.get_for_date(dest.id, date(2026, 7, 1)).id == wf.id

    wf.weather_conflict = True
    session.flush()
    assert repo.get(wf.id).weather_conflict is True

    repo.delete(wf)
    assert repo.get_for_date(dest.id, date(2026, 7, 1)) is None


@pytest.mark.integration
def test_user_crud(session):
    repo = UserRepository(session)
    user = repo.add(User(email="a@example.com", password_hash="hash-placeholder"))
    assert repo.get_by_email("a@example.com").id == user.id

    user.password_hash = "new-placeholder"
    session.flush()
    assert repo.get(user.id).password_hash == "new-placeholder"

    repo.delete(user)
    assert repo.get_by_email("a@example.com") is None


@pytest.mark.integration
def test_trip_and_activity_crud(session):
    dest = _new_destination(session)
    trip_repo = TripRepository(session)
    trip = trip_repo.add(
        Trip(
            budget=Decimal("1500.00"),
            duration_days=4,
            travelers=2,
            transport_mode="car",
            preferences={"culture": 0.9},
            chosen_destination=dest.id,
        )
    )
    assert trip.created_at is not None  # server default
    assert trip_repo.get(trip.id).preferences == {"culture": 0.9}  # JSONB round-trip

    act_repo = TripActivityRepository(session)
    act = act_repo.add(
        TripActivity(trip_id=trip.id, day_number=1, slot_order=1, start_time=time(9, 0))
    )
    assert act.was_rescheduled is False  # server default
    assert [a.id for a in act_repo.list_for_trip(trip.id)] == [act.id]

    act.was_rescheduled = True
    session.flush()
    assert act_repo.get(act.id).was_rescheduled is True

    act_repo.delete(act)
    assert act_repo.list_for_trip(trip.id) == []

    trip_repo.delete(trip)
    assert trip_repo.get(trip.id) is None


@pytest.mark.integration
def test_api_cache_crud(session):
    repo = ApiCacheRepository(session)
    row = repo.add(
        ApiCache(
            provider="open-meteo",
            query_signature="forecast?lat=46&lon=14",
            response_body={"ok": True},
        )
    )
    assert row.fetched_at is not None  # server default
    found = repo.find("open-meteo", "forecast?lat=46&lon=14")
    assert found.id == row.id
    assert found.response_body == {"ok": True}  # JSONB round-trip
    # No expiry set -> long-lived -> counts as fresh.
    assert repo.find_fresh("open-meteo", "forecast?lat=46&lon=14").id == row.id

    row.response_body = {"ok": False}
    session.flush()
    assert repo.get(row.id).response_body == {"ok": False}

    repo.delete(row)
    assert repo.find("open-meteo", "forecast?lat=46&lon=14") is None
