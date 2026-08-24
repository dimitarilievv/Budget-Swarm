-- ============================================================================
-- BudgetSwarm — PostgreSQL schema (DDL)
-- Concrete implementation of the data model described in Section 20 of the
-- technical documentation.
--
-- Data lifecycle legend (see docs Section 20.3):
--   [REF]   static / reference data (rarely changes)
--   [RFR]   periodically refreshed data (scheduled ingestion)
--   [CACHE] short-lived cache (TTL-controlled)
--   [HIST]  historical data (append-only record of generated plans)
--
-- Target: PostgreSQL 14+
-- ============================================================================

-- Optional: run in a dedicated schema
-- CREATE SCHEMA IF NOT EXISTS budgetswarm;
-- SET search_path TO budgetswarm;

-- ----------------------------------------------------------------------------
-- ENUM-like reference values (kept as CHECK constraints for simplicity)
-- ----------------------------------------------------------------------------
-- transport modes:        'airplane' | 'car' | 'bus' | 'train'
-- indoor/outdoor classes:  'indoor'  | 'outdoor' | 'mixed'
-- data quality flags:      'real'    | 'cached'  | 'estimated'


-- ============================================================================
-- [REF] destinations — one row per candidate city
-- ============================================================================
CREATE TABLE destinations (
    id           SERIAL PRIMARY KEY,
    city         VARCHAR(120) NOT NULL,
    country      VARCHAR(120) NOT NULL,
    country_code CHAR(2),                      -- ISO 3166-1 alpha-2 (links to fuel_prices)
    latitude     DOUBLE PRECISION NOT NULL,
    longitude    DOUBLE PRECISION NOT NULL,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (city, country)
);

-- ============================================================================
-- [RFR] destination_features — normalized feature scores per destination
--       (the curated "Scout" feature store; scores expected in [0,1])
-- ============================================================================
CREATE TABLE destination_features (
    destination_id  INTEGER PRIMARY KEY REFERENCES destinations(id) ON DELETE CASCADE,
    cost_of_living  NUMERIC(6,3),             -- normalized index
    tourism_score   NUMERIC(4,3),
    culture_score   NUMERIC(4,3),
    nature_score    NUMERIC(4,3),
    nightlife_score NUMERIC(4,3),
    food_score      NUMERIC(4,3),
    season_score    NUMERIC(4,3),             -- suitability for the current travel window
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- ============================================================================
-- [RFR] fuel_prices — per-country fuel price (EU Oil Bulletin / fuel-prices.eu)
--       Used by TransportAgent for car cost. Keyed by country code.
-- ============================================================================
CREATE TABLE fuel_prices (
    country_code   CHAR(2) PRIMARY KEY,       -- ISO 3166-1 alpha-2
    petrol_e95     NUMERIC(6,3),              -- EUR per litre
    diesel         NUMERIC(6,3),              -- EUR per litre
    source         VARCHAR(80) NOT NULL DEFAULT 'EU Weekly Oil Bulletin',
    price_date     DATE NOT NULL,             -- the bulletin week this reflects
    updated_at     TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- ============================================================================
-- [RFR] accommodations — normalized lodging (e.g. from Inside Airbnb)
-- ============================================================================
CREATE TABLE accommodations (
    id              SERIAL PRIMARY KEY,
    destination_id  INTEGER NOT NULL REFERENCES destinations(id) ON DELETE CASCADE,
    name            VARCHAR(200),
    accom_type      VARCHAR(60),              -- e.g. 'entire_home', 'private_room', 'hotel'
    price_per_night NUMERIC(10,2) NOT NULL,   -- EUR
    rating          NUMERIC(3,2),             -- review-derived quality signal
    latitude        DOUBLE PRECISION,
    longitude       DOUBLE PRECISION,
    quality_flag    VARCHAR(12) NOT NULL DEFAULT 'real'
                     CHECK (quality_flag IN ('real','cached','estimated')),
    snapshot_label  VARCHAR(60),              -- e.g. 'inside-airbnb-2026-06'
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX idx_accommodations_destination ON accommodations(destination_id);
CREATE INDEX idx_accommodations_price       ON accommodations(destination_id, price_per_night);

-- ============================================================================
-- [RFR] attractions — POIs per destination with indoor/outdoor class
-- ============================================================================
CREATE TABLE attractions (
    id              SERIAL PRIMARY KEY,
    destination_id  INTEGER NOT NULL REFERENCES destinations(id) ON DELETE CASCADE,
    name            VARCHAR(200) NOT NULL,
    category        VARCHAR(80),              -- e.g. 'museum','park','viewpoint','market'
    latitude        DOUBLE PRECISION,
    longitude       DOUBLE PRECISION,
    indoor_outdoor  VARCHAR(10) NOT NULL DEFAULT 'mixed'
                     CHECK (indoor_outdoor IN ('indoor','outdoor','mixed')),
    opening_hours   VARCHAR(200),             -- raw/normalized where available (nullable)
    popularity      NUMERIC(4,3),             -- where available (nullable)
    est_duration_min INTEGER,                 -- typical visit duration in minutes
    wiki_summary    TEXT,                     -- enrichment (Wikipedia)
    source          VARCHAR(40),              -- 'opentripmap' | 'overpass'
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX idx_attractions_destination ON attractions(destination_id);
CREATE INDEX idx_attractions_class       ON attractions(destination_id, indoor_outdoor);

-- ============================================================================
-- [CACHE] transport_options — cached/normalized transport results
--         Keyed conceptually by (origin, destination, mode). Origin is stored
--         as free text/coords because it comes from the user, not the DB.
-- ============================================================================
CREATE TABLE transport_options (
    id               SERIAL PRIMARY KEY,
    origin_label     VARCHAR(160) NOT NULL,   -- e.g. 'Skopje'
    destination_id   INTEGER NOT NULL REFERENCES destinations(id) ON DELETE CASCADE,
    mode             VARCHAR(10) NOT NULL
                      CHECK (mode IN ('airplane','car','bus','train')),
    cost             NUMERIC(10,2),           -- EUR (per the party/params used)
    duration_min     INTEGER,                 -- minutes
    distance_km      NUMERIC(10,2),           -- for car mode
    route_summary    JSONB,                   -- provider-agnostic route info
    quality_flag     VARCHAR(12) NOT NULL DEFAULT 'real'
                      CHECK (quality_flag IN ('real','cached','estimated')),
    fetched_at       TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX idx_transport_lookup ON transport_options(origin_label, destination_id, mode);

-- ============================================================================
-- [CACHE] weather_forecasts — cached forecasts per destination + date
-- ============================================================================
CREATE TABLE weather_forecasts (
    id              SERIAL PRIMARY KEY,
    destination_id  INTEGER NOT NULL REFERENCES destinations(id) ON DELETE CASCADE,
    forecast_date   DATE NOT NULL,
    temp_min        NUMERIC(5,2),
    temp_max        NUMERIC(5,2),
    precipitation   NUMERIC(6,2),             -- mm
    precip_prob     NUMERIC(4,1),             -- %
    wind_speed      NUMERIC(6,2),             -- km/h
    conditions      VARCHAR(60),              -- textual/coded condition
    weather_conflict BOOLEAN NOT NULL DEFAULT false,  -- derived risk flag for outdoor
    fetched_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (destination_id, forecast_date)
);

-- ============================================================================
-- [REF/HIST] users — OPTIONAL: only if accounts are implemented
-- ============================================================================
CREATE TABLE users (
    id            SERIAL PRIMARY KEY,
    email         VARCHAR(255) UNIQUE NOT NULL,
    password_hash VARCHAR(255) NOT NULL,       -- if not using an external identity provider
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- ============================================================================
-- [HIST] trips — a generated plan (inputs + chosen destination + totals)
-- ============================================================================
CREATE TABLE trips (
    id                  SERIAL PRIMARY KEY,
    user_id             INTEGER REFERENCES users(id) ON DELETE SET NULL,  -- nullable = anonymous
    -- inputs
    budget              NUMERIC(10,2) NOT NULL,
    duration_days       INTEGER NOT NULL,
    travelers           INTEGER NOT NULL,
    transport_mode      VARCHAR(10) NOT NULL
                         CHECK (transport_mode IN ('airplane','car','bus','train')),
    preferences         JSONB NOT NULL,        -- e.g. {"culture":0.9,"food":0.8,...}
    origin_label        VARCHAR(160),
    -- chosen result
    chosen_destination  INTEGER REFERENCES destinations(id) ON DELETE SET NULL,
    total_cost          NUMERIC(10,2),
    transport_cost      NUMERIC(10,2),
    accommodation_cost  NUMERIC(10,2),
    activities_cost     NUMERIC(10,2),
    overall_score       NUMERIC(8,4),
    explanation         TEXT,                  -- LLM-generated (nullable)
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX idx_trips_user ON trips(user_id);

-- ============================================================================
-- [HIST] trip_activities — the ordered activities belonging to a trip
-- ============================================================================
CREATE TABLE trip_activities (
    id             SERIAL PRIMARY KEY,
    trip_id        INTEGER NOT NULL REFERENCES trips(id) ON DELETE CASCADE,
    attraction_id  INTEGER REFERENCES attractions(id) ON DELETE SET NULL,
    day_number     INTEGER NOT NULL,          -- 1..duration_days
    slot_order     INTEGER NOT NULL,          -- ordering within the day
    start_time     TIME,                      -- where scheduled
    end_time       TIME,
    was_rescheduled BOOLEAN NOT NULL DEFAULT false,  -- true if moved due to weather
    UNIQUE (trip_id, day_number, slot_order)
);
CREATE INDEX idx_trip_activities_trip ON trip_activities(trip_id);

-- ============================================================================
-- [CACHE] api_cache — generic provider cache (see Section 23)
--         One simple table for "don't call the provider if we asked recently".
-- ============================================================================
CREATE TABLE api_cache (
    id               SERIAL PRIMARY KEY,
    provider         VARCHAR(60) NOT NULL,     -- e.g. 'travelpayouts','ors','open-meteo'
    query_signature  VARCHAR(400) NOT NULL,    -- hash/string of the request params
    response_body    JSONB NOT NULL,
    fetched_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
    expires_at       TIMESTAMPTZ,              -- TTL; NULL = long-lived reference
    UNIQUE (provider, query_signature)
);
CREATE INDEX idx_api_cache_expiry ON api_cache(provider, expires_at);

-- ============================================================================
-- Notes:
--  * All monetary values are stored in EUR after normalization.
--  * quality_flag columns let the optimizer and UI stay honest about whether a
--    value is real, cached, or estimated (see docs Section 22).
--  * transport_options and weather_forecasts are treated as cache: purge/refresh
--    by fetched_at / expires_at rather than keeping forever.
-- ============================================================================
