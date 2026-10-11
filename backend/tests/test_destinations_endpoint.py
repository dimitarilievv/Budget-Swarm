"""Integration test: GET /api/destinations through the session dependency (BS-028).

Proves the ``get_db`` session dependency and the repository layer work end-to-end
from a FastAPI route. The dependency is overridden to bind to the migrated test
database; the endpoint then reads seeded rows through the repository.

Marked ``integration`` and skipped automatically when no database is reachable.
In CI the backend job provisions a Postgres service, so this runs for real.
"""

from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from config import get_settings
from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session

BACKEND_DIR = Path(__file__).resolve().parents[1]


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


@pytest.mark.integration
def test_list_destinations_endpoint(migrated_engine):
    from db.session import get_db
    from fastapi.testclient import TestClient
    from main import app

    with migrated_engine.begin() as conn:
        conn.execute(text("""
                INSERT INTO destinations (city, country, country_code, latitude, longitude)
                VALUES ('Porto', 'Portugal', 'PT', 41.1579, -8.6291)
                """))

    def _override_get_db():
        db = Session(bind=migrated_engine)
        try:
            yield db
        finally:
            db.close()

    app.dependency_overrides[get_db] = _override_get_db
    try:
        client = TestClient(app)
        resp = client.get("/api/destinations")
        assert resp.status_code == 200
        rows = resp.json()
        porto = next(d for d in rows if d["city"] == "Porto")
        assert porto["country"] == "Portugal"
        assert porto["country_code"] == "PT"
        assert porto["latitude"] == pytest.approx(41.1579)
    finally:
        app.dependency_overrides.clear()


@pytest.mark.integration
def test_list_destinations_excludes_inactive(migrated_engine):
    from db.session import get_db
    from fastapi.testclient import TestClient
    from main import app

    with migrated_engine.begin() as conn:
        conn.execute(text("""
                INSERT INTO destinations (city, country, country_code, latitude, longitude, active)
                VALUES ('Activeville', 'Testland', 'TL', 1.0, 2.0, true),
                       ('Hiddentown', 'Testland', 'TL', 3.0, 4.0, false)
                """))

    def _override_get_db():
        db = Session(bind=migrated_engine)
        try:
            yield db
        finally:
            db.close()

    app.dependency_overrides[get_db] = _override_get_db
    try:
        rows = TestClient(app).get("/api/destinations").json()
        cities = {d["city"] for d in rows}
        assert "Activeville" in cities
        assert "Hiddentown" not in cities  # inactive is excluded
    finally:
        app.dependency_overrides.clear()


@pytest.mark.integration
def test_set_destination_active_toggles_candidate_pool(migrated_engine):
    from db.repositories import DestinationRepository
    from scripts.set_destination_active import set_active

    with migrated_engine.begin() as conn:
        conn.execute(text("""
                INSERT INTO destinations (city, country, country_code, latitude, longitude)
                VALUES ('Togglegrad', 'Testland', 'TL', 5.0, 6.0)
                """))

    with Session(migrated_engine) as session:
        repo = DestinationRepository(session)
        assert any(d.city == "Togglegrad" for d in repo.list_active())

        # Disable → dropped from the active pool, but the row still exists.
        changed, not_found, ambiguous = set_active(session, ["Togglegrad"], active=False)
        assert changed == ["Togglegrad, Testland"]
        assert not_found == [] and ambiguous == []

    with Session(migrated_engine) as session:
        repo = DestinationRepository(session)
        assert not any(d.city == "Togglegrad" for d in repo.list_active())
        assert repo.get_by_city_country("Togglegrad", "Testland") is not None  # not deleted

        # Re-enable → back in the pool.
        set_active(session, ["Togglegrad"], active=True)

    with Session(migrated_engine) as session:
        assert any(d.city == "Togglegrad" for d in DestinationRepository(session).list_active())


@pytest.mark.integration
def test_set_destination_active_reports_unmatched(migrated_engine):
    from scripts.set_destination_active import set_active

    with Session(migrated_engine) as session:
        changed, not_found, ambiguous = set_active(session, ["Nowhereville"], active=False)
        assert changed == []
        assert not_found == ["Nowhereville"]
