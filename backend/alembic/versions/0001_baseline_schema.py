"""baseline schema (BS-018)

Baseline migration mirroring docs/schema.sql. The DDL is inlined here so the
migration is self-contained and versioned — it does not depend on the docs file
changing over time. Keep this in sync with docs/schema.sql until ORM models
(BS-028) become the source of truth.

Revision ID: 0001_baseline
Revises:
Create Date: 2026-08-24

"""

from collections.abc import Sequence

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0001_baseline"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


SCHEMA_DDL = """
CREATE TABLE destinations (
    id           SERIAL PRIMARY KEY,
    city         VARCHAR(120) NOT NULL,
    country      VARCHAR(120) NOT NULL,
    country_code CHAR(2),
    latitude     DOUBLE PRECISION NOT NULL,
    longitude    DOUBLE PRECISION NOT NULL,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (city, country)
);

CREATE TABLE destination_features (
    destination_id  INTEGER PRIMARY KEY REFERENCES destinations(id) ON DELETE CASCADE,
    cost_of_living  NUMERIC(6,3),
    tourism_score   NUMERIC(4,3),
    culture_score   NUMERIC(4,3),
    nature_score    NUMERIC(4,3),
    nightlife_score NUMERIC(4,3),
    food_score      NUMERIC(4,3),
    season_score    NUMERIC(4,3),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE fuel_prices (
    country_code   CHAR(2) PRIMARY KEY,
    petrol_e95     NUMERIC(6,3),
    diesel         NUMERIC(6,3),
    source         VARCHAR(80) NOT NULL DEFAULT 'EU Weekly Oil Bulletin',
    price_date     DATE NOT NULL,
    updated_at     TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE accommodations (
    id              SERIAL PRIMARY KEY,
    destination_id  INTEGER NOT NULL REFERENCES destinations(id) ON DELETE CASCADE,
    name            VARCHAR(200),
    accom_type      VARCHAR(60),
    price_per_night NUMERIC(10,2) NOT NULL,
    rating          NUMERIC(3,2),
    latitude        DOUBLE PRECISION,
    longitude       DOUBLE PRECISION,
    quality_flag    VARCHAR(12) NOT NULL DEFAULT 'real'
                     CHECK (quality_flag IN ('real','cached','estimated')),
    snapshot_label  VARCHAR(60),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX idx_accommodations_destination ON accommodations(destination_id);
CREATE INDEX idx_accommodations_price       ON accommodations(destination_id, price_per_night);

CREATE TABLE attractions (
    id              SERIAL PRIMARY KEY,
    destination_id  INTEGER NOT NULL REFERENCES destinations(id) ON DELETE CASCADE,
    name            VARCHAR(200) NOT NULL,
    category        VARCHAR(80),
    latitude        DOUBLE PRECISION,
    longitude       DOUBLE PRECISION,
    indoor_outdoor  VARCHAR(10) NOT NULL DEFAULT 'mixed'
                     CHECK (indoor_outdoor IN ('indoor','outdoor','mixed')),
    opening_hours   VARCHAR(200),
    popularity      NUMERIC(4,3),
    est_duration_min INTEGER,
    wiki_summary    TEXT,
    source          VARCHAR(40),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX idx_attractions_destination ON attractions(destination_id);
CREATE INDEX idx_attractions_class       ON attractions(destination_id, indoor_outdoor);

CREATE TABLE transport_options (
    id               SERIAL PRIMARY KEY,
    origin_label     VARCHAR(160) NOT NULL,
    destination_id   INTEGER NOT NULL REFERENCES destinations(id) ON DELETE CASCADE,
    mode             VARCHAR(10) NOT NULL
                      CHECK (mode IN ('airplane','car','bus','train')),
    cost             NUMERIC(10,2),
    duration_min     INTEGER,
    distance_km      NUMERIC(10,2),
    route_summary    JSONB,
    quality_flag     VARCHAR(12) NOT NULL DEFAULT 'real'
                      CHECK (quality_flag IN ('real','cached','estimated')),
    fetched_at       TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX idx_transport_lookup ON transport_options(origin_label, destination_id, mode);

CREATE TABLE weather_forecasts (
    id              SERIAL PRIMARY KEY,
    destination_id  INTEGER NOT NULL REFERENCES destinations(id) ON DELETE CASCADE,
    forecast_date   DATE NOT NULL,
    temp_min        NUMERIC(5,2),
    temp_max        NUMERIC(5,2),
    precipitation   NUMERIC(6,2),
    precip_prob     NUMERIC(4,1),
    wind_speed      NUMERIC(6,2),
    conditions      VARCHAR(60),
    weather_conflict BOOLEAN NOT NULL DEFAULT false,
    fetched_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (destination_id, forecast_date)
);

CREATE TABLE users (
    id            SERIAL PRIMARY KEY,
    email         VARCHAR(255) UNIQUE NOT NULL,
    password_hash VARCHAR(255) NOT NULL,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE trips (
    id                  SERIAL PRIMARY KEY,
    user_id             INTEGER REFERENCES users(id) ON DELETE SET NULL,
    budget              NUMERIC(10,2) NOT NULL,
    duration_days       INTEGER NOT NULL,
    travelers           INTEGER NOT NULL,
    transport_mode      VARCHAR(10) NOT NULL
                         CHECK (transport_mode IN ('airplane','car','bus','train')),
    preferences         JSONB NOT NULL,
    origin_label        VARCHAR(160),
    chosen_destination  INTEGER REFERENCES destinations(id) ON DELETE SET NULL,
    total_cost          NUMERIC(10,2),
    transport_cost      NUMERIC(10,2),
    accommodation_cost  NUMERIC(10,2),
    activities_cost     NUMERIC(10,2),
    overall_score       NUMERIC(8,4),
    explanation         TEXT,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX idx_trips_user ON trips(user_id);

CREATE TABLE trip_activities (
    id             SERIAL PRIMARY KEY,
    trip_id        INTEGER NOT NULL REFERENCES trips(id) ON DELETE CASCADE,
    attraction_id  INTEGER REFERENCES attractions(id) ON DELETE SET NULL,
    day_number     INTEGER NOT NULL,
    slot_order     INTEGER NOT NULL,
    start_time     TIME,
    end_time       TIME,
    was_rescheduled BOOLEAN NOT NULL DEFAULT false,
    UNIQUE (trip_id, day_number, slot_order)
);
CREATE INDEX idx_trip_activities_trip ON trip_activities(trip_id);

CREATE TABLE api_cache (
    id               SERIAL PRIMARY KEY,
    provider         VARCHAR(60) NOT NULL,
    query_signature  VARCHAR(400) NOT NULL,
    response_body    JSONB NOT NULL,
    fetched_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
    expires_at       TIMESTAMPTZ,
    UNIQUE (provider, query_signature)
);
CREATE INDEX idx_api_cache_expiry ON api_cache(provider, expires_at);
"""

# Reverse dependency order for a clean teardown.
_TABLES = [
    "api_cache",
    "trip_activities",
    "trips",
    "users",
    "weather_forecasts",
    "transport_options",
    "attractions",
    "accommodations",
    "fuel_prices",
    "destination_features",
    "destinations",
]


def upgrade() -> None:
    op.get_bind().exec_driver_sql(SCHEMA_DDL)


def downgrade() -> None:
    bind = op.get_bind()
    for table in _TABLES:
        bind.exec_driver_sql(f"DROP TABLE IF EXISTS {table} CASCADE;")
