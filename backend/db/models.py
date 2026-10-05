"""SQLAlchemy ORM models for every MVP table (BS-028).

These map the tables defined in docs/schema.sql (created by the Alembic baseline
migration in BS-018) onto the declarative ``Base`` from ``db.session``. The
migration remains the source of truth for the DDL; these models are the typed
read/write surface used by the repository layer (``db.repositories``) so backend
and agents never scatter raw SQL.

Column server defaults (``now()``, ``'real'``, ``false`` …) are declared so that
inserting through the ORM lets the database fill them, exactly as a raw INSERT
would. Foreign-key cascades are enforced by the database; relationships use
``passive_deletes=True`` so the ORM defers to those rules rather than emulating
them.
"""

from __future__ import annotations

from datetime import date, datetime, time
from decimal import Decimal
from typing import Any

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    Date,
    DateTime,
    Double,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    Time,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from db.session import Base

# Reused literal fragments for the quality-flag CHECK constraints.
_QUALITY_FLAGS = "('real','cached','estimated')"
_TRANSPORT_MODES = "('airplane','car','bus','train')"


class Destination(Base):
    """[REF] One row per candidate city."""

    __tablename__ = "destinations"
    __table_args__ = (UniqueConstraint("city", "country", name="destinations_city_country_key"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    city: Mapped[str] = mapped_column(String(120), nullable=False)
    country: Mapped[str] = mapped_column(String(120), nullable=False)
    country_code: Mapped[str | None] = mapped_column(String(2))
    latitude: Mapped[float] = mapped_column(Double, nullable=False)
    longitude: Mapped[float] = mapped_column(Double, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    features: Mapped[DestinationFeature | None] = relationship(
        back_populates="destination", uselist=False, passive_deletes=True
    )
    accommodations: Mapped[list[Accommodation]] = relationship(
        back_populates="destination", passive_deletes=True
    )
    attractions: Mapped[list[Attraction]] = relationship(
        back_populates="destination", passive_deletes=True
    )


class DestinationFeature(Base):
    """[RFR] Normalized feature scores per destination (the Scout feature store)."""

    __tablename__ = "destination_features"

    destination_id: Mapped[int] = mapped_column(
        ForeignKey("destinations.id", ondelete="CASCADE"), primary_key=True
    )
    cost_of_living: Mapped[Decimal | None] = mapped_column(Numeric(6, 3))
    tourism_score: Mapped[Decimal | None] = mapped_column(Numeric(4, 3))
    culture_score: Mapped[Decimal | None] = mapped_column(Numeric(4, 3))
    nature_score: Mapped[Decimal | None] = mapped_column(Numeric(4, 3))
    nightlife_score: Mapped[Decimal | None] = mapped_column(Numeric(4, 3))
    food_score: Mapped[Decimal | None] = mapped_column(Numeric(4, 3))
    season_score: Mapped[Decimal | None] = mapped_column(Numeric(4, 3))
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    destination: Mapped[Destination] = relationship(back_populates="features")


class FuelPrice(Base):
    """[RFR] Per-country fuel price (EU Oil Bulletin), keyed by country code."""

    __tablename__ = "fuel_prices"

    country_code: Mapped[str] = mapped_column(String(2), primary_key=True)
    petrol_e95: Mapped[Decimal | None] = mapped_column(Numeric(6, 3))
    diesel: Mapped[Decimal | None] = mapped_column(Numeric(6, 3))
    source: Mapped[str] = mapped_column(
        String(80), nullable=False, server_default=text("'EU Weekly Oil Bulletin'")
    )
    price_date: Mapped[date] = mapped_column(Date, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class Accommodation(Base):
    """[RFR] Normalized lodging (e.g. from Inside Airbnb)."""

    __tablename__ = "accommodations"
    __table_args__ = (
        CheckConstraint(
            f"quality_flag IN {_QUALITY_FLAGS}", name="accommodations_quality_flag_check"
        ),
        Index("idx_accommodations_destination", "destination_id"),
        Index("idx_accommodations_price", "destination_id", "price_per_night"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    destination_id: Mapped[int] = mapped_column(
        ForeignKey("destinations.id", ondelete="CASCADE"), nullable=False
    )
    name: Mapped[str | None] = mapped_column(String(200))
    accom_type: Mapped[str | None] = mapped_column(String(60))
    price_per_night: Mapped[Decimal] = mapped_column(Numeric(10, 2), nullable=False)
    rating: Mapped[Decimal | None] = mapped_column(Numeric(3, 2))
    latitude: Mapped[float | None] = mapped_column(Double)
    longitude: Mapped[float | None] = mapped_column(Double)
    quality_flag: Mapped[str] = mapped_column(
        String(12), nullable=False, server_default=text("'real'")
    )
    snapshot_label: Mapped[str | None] = mapped_column(String(60))
    # Listing detail carried from the source row (BS-104) for a richer UI card; all
    # optional (a price-stats-estimate option has none of them).
    listing_url: Mapped[str | None] = mapped_column(String(300))
    picture_url: Mapped[str | None] = mapped_column(String(500))
    review_count: Mapped[int | None] = mapped_column(Integer)
    location_score: Mapped[Decimal | None] = mapped_column(Numeric(3, 2))
    neighbourhood: Mapped[str | None] = mapped_column(String(120))
    amenities: Mapped[list[str] | None] = mapped_column(JSONB)
    accommodates: Mapped[int | None] = mapped_column(Integer)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    destination: Mapped[Destination] = relationship(back_populates="accommodations")


class AccommodationPriceStats(Base):
    """[RFR] Precomputed per-destination EUR price distribution (BS-035).

    One row per destination: the count and min/Q1/median/Q3/max of its
    normalized ``accommodations`` prices (EUR). Used by StayAgent for
    representative-option selection and, for cities without listings, as the
    ``estimated`` price fallback.
    """

    __tablename__ = "accommodation_price_stats"

    destination_id: Mapped[int] = mapped_column(
        ForeignKey("destinations.id", ondelete="CASCADE"), primary_key=True
    )
    sample_size: Mapped[int] = mapped_column(Integer, nullable=False)
    currency: Mapped[str] = mapped_column(String(3), nullable=False, server_default=text("'EUR'"))
    price_min: Mapped[Decimal | None] = mapped_column(Numeric(10, 2))
    price_q1: Mapped[Decimal | None] = mapped_column(Numeric(10, 2))
    price_median: Mapped[Decimal | None] = mapped_column(Numeric(10, 2))
    price_q3: Mapped[Decimal | None] = mapped_column(Numeric(10, 2))
    price_max: Mapped[Decimal | None] = mapped_column(Numeric(10, 2))
    snapshot_label: Mapped[str | None] = mapped_column(String(60))
    computed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class Attraction(Base):
    """[RFR] POIs per destination with an indoor/outdoor class."""

    __tablename__ = "attractions"
    __table_args__ = (
        CheckConstraint(
            "indoor_outdoor IN ('indoor','outdoor','mixed')",
            name="attractions_indoor_outdoor_check",
        ),
        Index("idx_attractions_destination", "destination_id"),
        Index("idx_attractions_class", "destination_id", "indoor_outdoor"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    destination_id: Mapped[int] = mapped_column(
        ForeignKey("destinations.id", ondelete="CASCADE"), nullable=False
    )
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    category: Mapped[str | None] = mapped_column(String(80))
    latitude: Mapped[float | None] = mapped_column(Double)
    longitude: Mapped[float | None] = mapped_column(Double)
    indoor_outdoor: Mapped[str] = mapped_column(
        String(10), nullable=False, server_default=text("'mixed'")
    )
    opening_hours: Mapped[str | None] = mapped_column(String(200))
    popularity: Mapped[Decimal | None] = mapped_column(Numeric(4, 3))
    est_duration_min: Mapped[int | None] = mapped_column(Integer)
    wiki_summary: Mapped[str | None] = mapped_column(Text)
    source: Mapped[str | None] = mapped_column(String(40))
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    destination: Mapped[Destination] = relationship(back_populates="attractions")


class TransportOption(Base):
    """[CACHE] Cached/normalized transport results per (origin, destination, mode)."""

    __tablename__ = "transport_options"
    __table_args__ = (
        CheckConstraint(f"mode IN {_TRANSPORT_MODES}", name="transport_options_mode_check"),
        CheckConstraint(
            f"quality_flag IN {_QUALITY_FLAGS}", name="transport_options_quality_flag_check"
        ),
        Index("idx_transport_lookup", "origin_label", "destination_id", "mode"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    origin_label: Mapped[str] = mapped_column(String(160), nullable=False)
    destination_id: Mapped[int] = mapped_column(
        ForeignKey("destinations.id", ondelete="CASCADE"), nullable=False
    )
    mode: Mapped[str] = mapped_column(String(10), nullable=False)
    cost: Mapped[Decimal | None] = mapped_column(Numeric(10, 2))
    duration_min: Mapped[int | None] = mapped_column(Integer)
    distance_km: Mapped[Decimal | None] = mapped_column(Numeric(10, 2))
    route_summary: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    quality_flag: Mapped[str] = mapped_column(
        String(12), nullable=False, server_default=text("'real'")
    )
    fetched_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class WeatherForecast(Base):
    """[CACHE] Cached forecasts per destination + date with a derived conflict flag."""

    __tablename__ = "weather_forecasts"
    __table_args__ = (
        UniqueConstraint(
            "destination_id",
            "forecast_date",
            name="weather_forecasts_destination_id_forecast_date_key",
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    destination_id: Mapped[int] = mapped_column(
        ForeignKey("destinations.id", ondelete="CASCADE"), nullable=False
    )
    forecast_date: Mapped[date] = mapped_column(Date, nullable=False)
    temp_min: Mapped[Decimal | None] = mapped_column(Numeric(5, 2))
    temp_max: Mapped[Decimal | None] = mapped_column(Numeric(5, 2))
    precipitation: Mapped[Decimal | None] = mapped_column(Numeric(6, 2))
    precip_prob: Mapped[Decimal | None] = mapped_column(Numeric(4, 1))
    wind_speed: Mapped[Decimal | None] = mapped_column(Numeric(6, 2))
    conditions: Mapped[str | None] = mapped_column(String(60))
    weather_conflict: Mapped[bool] = mapped_column(nullable=False, server_default=text("false"))
    fetched_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class User(Base):
    """[REF/HIST] Optional authenticated account (accounts in scope — see ADR-012)."""

    __tablename__ = "users"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    email: Mapped[str] = mapped_column(String(255), nullable=False, unique=True)
    password_hash: Mapped[str] = mapped_column(String(255), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    trips: Mapped[list[Trip]] = relationship(back_populates="user")


class Trip(Base):
    """[HIST] A generated plan: inputs + chosen destination + totals."""

    __tablename__ = "trips"
    __table_args__ = (
        CheckConstraint(f"transport_mode IN {_TRANSPORT_MODES}", name="trips_transport_mode_check"),
        Index("idx_trips_user", "user_id"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    budget: Mapped[Decimal] = mapped_column(Numeric(10, 2), nullable=False)
    duration_days: Mapped[int] = mapped_column(Integer, nullable=False)
    travelers: Mapped[int] = mapped_column(Integer, nullable=False)
    transport_mode: Mapped[str] = mapped_column(String(10), nullable=False)
    preferences: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    origin_label: Mapped[str | None] = mapped_column(String(160))
    chosen_destination: Mapped[int | None] = mapped_column(
        ForeignKey("destinations.id", ondelete="SET NULL")
    )
    total_cost: Mapped[Decimal | None] = mapped_column(Numeric(10, 2))
    transport_cost: Mapped[Decimal | None] = mapped_column(Numeric(10, 2))
    accommodation_cost: Mapped[Decimal | None] = mapped_column(Numeric(10, 2))
    activities_cost: Mapped[Decimal | None] = mapped_column(Numeric(10, 2))
    overall_score: Mapped[Decimal | None] = mapped_column(Numeric(8, 4))
    explanation: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    user: Mapped[User | None] = relationship(back_populates="trips")
    activities: Mapped[list[TripActivity]] = relationship(
        back_populates="trip", passive_deletes=True
    )


class TripActivity(Base):
    """[HIST] The ordered activities belonging to a trip."""

    __tablename__ = "trip_activities"
    __table_args__ = (
        UniqueConstraint(
            "trip_id",
            "day_number",
            "slot_order",
            name="trip_activities_trip_id_day_number_slot_order_key",
        ),
        Index("idx_trip_activities_trip", "trip_id"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    trip_id: Mapped[int] = mapped_column(ForeignKey("trips.id", ondelete="CASCADE"), nullable=False)
    attraction_id: Mapped[int | None] = mapped_column(
        ForeignKey("attractions.id", ondelete="SET NULL")
    )
    day_number: Mapped[int] = mapped_column(Integer, nullable=False)
    slot_order: Mapped[int] = mapped_column(Integer, nullable=False)
    start_time: Mapped[time | None] = mapped_column(Time)
    end_time: Mapped[time | None] = mapped_column(Time)
    was_rescheduled: Mapped[bool] = mapped_column(nullable=False, server_default=text("false"))

    trip: Mapped[Trip] = relationship(back_populates="activities")
    attraction: Mapped[Attraction | None] = relationship()


class ApiCache(Base):
    """[CACHE] Generic provider cache keyed by (provider, query_signature) with a TTL."""

    __tablename__ = "api_cache"
    __table_args__ = (
        UniqueConstraint(
            "provider", "query_signature", name="api_cache_provider_query_signature_key"
        ),
        Index("idx_api_cache_expiry", "provider", "expires_at"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    provider: Mapped[str] = mapped_column(String(60), nullable=False)
    query_signature: Mapped[str] = mapped_column(String(400), nullable=False)
    response_body: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    fetched_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class RawAccommodationListing(Base):
    """[RAW] One accommodation listing as downloaded, before cleaning (BS-033).

    Source-agnostic: ``source`` names the provider (e.g. ``inside-airbnb``) and the
    full CSV row is preserved in ``raw`` (JSONB), so any source's schema fits with
    no table change. ``snapshot_label`` (``<source>-<city>-<date>``) records the
    exact snapshot; ``(snapshot_label, listing_id)`` is unique so re-importing a
    snapshot upserts in place. BS-034 reads this table to normalize the usable rows
    into :class:`Accommodation`.
    """

    __tablename__ = "raw_accommodation_listings"
    __table_args__ = (
        UniqueConstraint(
            "snapshot_label",
            "listing_id",
            name="raw_accommodation_listings_snapshot_label_listing_id_key",
        ),
        Index("idx_raw_accom_source_snapshot", "source", "snapshot_label"),
        Index("idx_raw_accom_city", "city"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    source: Mapped[str] = mapped_column(String(40), nullable=False)
    snapshot_label: Mapped[str] = mapped_column(String(60), nullable=False)
    city: Mapped[str] = mapped_column(String(80), nullable=False)
    snapshot_date: Mapped[date] = mapped_column(Date, nullable=False)
    listing_id: Mapped[str] = mapped_column(String(64), nullable=False)
    raw: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    loaded_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


__all__ = [
    "Accommodation",
    "AccommodationPriceStats",
    "ApiCache",
    "Attraction",
    "Destination",
    "DestinationFeature",
    "FuelPrice",
    "RawAccommodationListing",
    "TransportOption",
    "Trip",
    "TripActivity",
    "User",
    "WeatherForecast",
]
