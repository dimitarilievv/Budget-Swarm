"""Repository/session layer over the ORM models (BS-028).

A thin repository per table gives backend and agents a single, typed place to
read and write, instead of scattering raw SQL. Each repository wraps a
SQLAlchemy ``Session`` (obtained from the FastAPI ``get_db`` dependency in
``db.session``) and exposes generic CRUD plus a few table-specific finders.

Write methods ``flush`` (not ``commit``) so several repository calls compose
inside one request/transaction; committing is the caller's decision — typically
the ``get_db`` request scope or an explicit ``session.commit()``.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Generic, TypeVar

from sqlalchemy import delete, func, or_, select
from sqlalchemy.orm import Session

from db.models import (
    Accommodation,
    AccommodationPriceStats,
    ApiCache,
    Attraction,
    Destination,
    DestinationFeature,
    FuelPrice,
    RawAccommodationListing,
    RefreshToken,
    TransportOption,
    Trip,
    TripActivity,
    User,
    WeatherForecast,
)
from db.session import Base

logger = logging.getLogger(__name__)

ModelT = TypeVar("ModelT", bound=Base)


class Repository(Generic[ModelT]):
    """Generic CRUD over a single mapped model.

    Subclasses set ``model``; instances hold the active ``Session``.
    """

    model: type[ModelT]

    def __init__(self, session: Session) -> None:
        self.session = session

    def add(self, obj: ModelT) -> ModelT:
        """Persist a new (or attached) instance and flush so its PK is populated."""
        self.session.add(obj)
        self.session.flush()
        return obj

    def get(self, pk: object) -> ModelT | None:
        """Return the row by primary key, or None."""
        return self.session.get(self.model, pk)

    def list(self, *, limit: int | None = None, offset: int | None = None) -> list[ModelT]:
        """Return rows, optionally paginated."""
        stmt = select(self.model)
        if offset is not None:
            stmt = stmt.offset(offset)
        if limit is not None:
            stmt = stmt.limit(limit)
        return list(self.session.scalars(stmt).all())

    def count(self) -> int:
        """Return the number of rows in the table."""
        return self.session.scalar(select(func.count()).select_from(self.model)) or 0

    def delete(self, obj: ModelT) -> None:
        """Delete an instance and flush."""
        self.session.delete(obj)
        self.session.flush()


class DestinationRepository(Repository[Destination]):
    model = Destination

    def get_by_city_country(self, city: str, country: str) -> Destination | None:
        stmt = select(Destination).where(Destination.city == city, Destination.country == country)
        return self.session.scalars(stmt).first()

    def list_active(self, *, limit: int | None = None) -> list[Destination]:
        """Return only active destinations (the candidate pool / public listing).

        Inactive cities are soft-disabled: still in the table (with their data) but
        excluded here, so Scout never shortlists them and the API never lists them.
        """
        stmt = select(Destination).where(Destination.active.is_(True))
        if limit is not None:
            stmt = stmt.limit(limit)
        return list(self.session.scalars(stmt).all())


class DestinationFeatureRepository(Repository[DestinationFeature]):
    model = DestinationFeature


class FuelPriceRepository(Repository[FuelPrice]):
    model = FuelPrice

    def get_by_country(self, country_code: str) -> FuelPrice | None:
        return self.get(country_code)


class AccommodationRepository(Repository[Accommodation]):
    model = Accommodation

    def list_by_destination(self, destination_id: int) -> list[Accommodation]:
        stmt = (
            select(Accommodation)
            .where(Accommodation.destination_id == destination_id)
            .order_by(Accommodation.price_per_night)
        )
        return list(self.session.scalars(stmt).all())


class AccommodationPriceStatsRepository(Repository[AccommodationPriceStats]):
    model = AccommodationPriceStats

    def get_for_destination(self, destination_id: int) -> AccommodationPriceStats | None:
        return self.get(destination_id)


class AttractionRepository(Repository[Attraction]):
    model = Attraction

    def list_by_destination(
        self, destination_id: int, *, indoor_outdoor: str | None = None
    ) -> list[Attraction]:
        stmt = select(Attraction).where(Attraction.destination_id == destination_id)
        if indoor_outdoor is not None:
            stmt = stmt.where(Attraction.indoor_outdoor == indoor_outdoor)
        return list(self.session.scalars(stmt).all())


class TransportOptionRepository(Repository[TransportOption]):
    model = TransportOption

    def find(self, origin_label: str, destination_id: int, mode: str) -> TransportOption | None:
        """Look up a cached option by its conceptual key, newest first."""
        stmt = (
            select(TransportOption)
            .where(
                TransportOption.origin_label == origin_label,
                TransportOption.destination_id == destination_id,
                TransportOption.mode == mode,
            )
            .order_by(TransportOption.fetched_at.desc())
        )
        return self.session.scalars(stmt).first()


class WeatherForecastRepository(Repository[WeatherForecast]):
    model = WeatherForecast

    def get_for_date(self, destination_id: int, forecast_date: object) -> WeatherForecast | None:
        stmt = select(WeatherForecast).where(
            WeatherForecast.destination_id == destination_id,
            WeatherForecast.forecast_date == forecast_date,
        )
        return self.session.scalars(stmt).first()


class UserRepository(Repository[User]):
    model = User

    def get_by_email(self, email: str) -> User | None:
        return self.session.scalars(select(User).where(User.email == email)).first()


class RefreshTokenRepository(Repository[RefreshToken]):
    model = RefreshToken

    def get_active_by_hash(self, token_hash: str, *, now: datetime) -> RefreshToken | None:
        """Return the matching token row only if it is neither revoked nor expired."""
        stmt = select(RefreshToken).where(
            RefreshToken.token_hash == token_hash,
            RefreshToken.revoked_at.is_(None),
            RefreshToken.expires_at > now,
        )
        return self.session.scalars(stmt).first()

    def revoke(self, token: RefreshToken, *, now: datetime) -> None:
        """Mark a token revoked (idempotent) and flush."""
        if token.revoked_at is None:
            token.revoked_at = now
        self.session.flush()

    def revoke_all_for_user(self, user_id: int, *, now: datetime) -> int:
        """Revoke every still-active token for a user; return how many were revoked."""
        stmt = select(RefreshToken).where(
            RefreshToken.user_id == user_id, RefreshToken.revoked_at.is_(None)
        )
        tokens = self.session.scalars(stmt).all()
        for token in tokens:
            token.revoked_at = now
        self.session.flush()
        return len(tokens)

    def purge_dead(self, *, now: datetime) -> int:
        """Delete tokens that can never authenticate again — expired or revoked.

        A row is unusable the moment it is past ``expires_at`` or has a
        ``revoked_at`` (``get_active_by_hash`` requires neither), so purging them
        keeps the table from growing without bound. Returns how many were removed.
        """
        stmt = (
            delete(RefreshToken).where(
                or_(RefreshToken.expires_at <= now, RefreshToken.revoked_at.is_not(None))
            )
            # Maintenance sweep: don't try to sync the ORM identity map (the default
            # "auto"/"evaluate" strategy can't evaluate this WHERE in Python).
            .execution_options(synchronize_session=False)
        )
        result = self.session.execute(stmt)
        self.session.flush()
        return result.rowcount or 0


class TripRepository(Repository[Trip]):
    model = Trip

    def list_for_user(self, user_id: int) -> list[Trip]:
        stmt = select(Trip).where(Trip.user_id == user_id).order_by(Trip.created_at.desc())
        return list(self.session.scalars(stmt).all())

    def get_for_user(self, trip_id: int, user_id: int) -> Trip | None:
        """Return the trip only if it exists *and* belongs to ``user_id``.

        Scoping the lookup by both keys is what lets the route answer **404** for a
        trip owned by someone else — identical to an unknown id — so ownership of a
        resource is never leaked (no 403 that would confirm it exists).
        """
        stmt = select(Trip).where(Trip.id == trip_id, Trip.user_id == user_id)
        return self.session.scalars(stmt).first()

    def delete_for_user(self, user_id: int) -> int:
        """Delete every trip owned by ``user_id``; return how many were removed.

        A bulk ``DELETE`` so the database's ``ON DELETE CASCADE`` on
        ``trip_activities.trip_id`` removes each trip's activities too. Used by
        account deletion, where the user's saved trips must go with them (the
        ``trips.user_id`` FK is ``SET NULL``, which would otherwise orphan them).
        """
        stmt = (
            delete(Trip).where(Trip.user_id == user_id).execution_options(synchronize_session=False)
        )
        result = self.session.execute(stmt)
        self.session.flush()
        return result.rowcount or 0


class TripActivityRepository(Repository[TripActivity]):
    model = TripActivity

    def list_for_trip(self, trip_id: int) -> list[TripActivity]:
        stmt = (
            select(TripActivity)
            .where(TripActivity.trip_id == trip_id)
            .order_by(TripActivity.day_number, TripActivity.slot_order)
        )
        return list(self.session.scalars(stmt).all())


class RawAccommodationListingRepository(Repository[RawAccommodationListing]):
    model = RawAccommodationListing

    def count_for_snapshot(self, snapshot_label: str) -> int:
        """Number of raw rows stored for one accommodation snapshot."""
        stmt = (
            select(func.count())
            .select_from(RawAccommodationListing)
            .where(RawAccommodationListing.snapshot_label == snapshot_label)
        )
        return self.session.scalar(stmt) or 0

    def list_for_snapshot(self, snapshot_label: str) -> list[RawAccommodationListing]:
        """All raw rows for one snapshot (BS-034 reads these to normalize)."""
        stmt = select(RawAccommodationListing).where(
            RawAccommodationListing.snapshot_label == snapshot_label
        )
        return list(self.session.scalars(stmt).all())


class ApiCacheRepository(Repository[ApiCache]):
    model = ApiCache

    def find(self, provider: str, query_signature: str) -> ApiCache | None:
        """Return the cache row for a provider/signature pair, if any."""
        stmt = select(ApiCache).where(
            ApiCache.provider == provider,
            ApiCache.query_signature == query_signature,
        )
        return self.session.scalars(stmt).first()

    def find_fresh(self, provider: str, query_signature: str) -> ApiCache | None:
        """Return the cached row only if it has not expired (NULL expiry = long-lived)."""
        now = datetime.now(UTC)
        stmt = select(ApiCache).where(
            ApiCache.provider == provider,
            ApiCache.query_signature == query_signature,
            (ApiCache.expires_at.is_(None)) | (ApiCache.expires_at > now),
        )
        return self.session.scalars(stmt).first()


__all__ = [
    "AccommodationPriceStatsRepository",
    "AccommodationRepository",
    "ApiCacheRepository",
    "AttractionRepository",
    "DestinationFeatureRepository",
    "DestinationRepository",
    "FuelPriceRepository",
    "RawAccommodationListingRepository",
    "Repository",
    "TransportOptionRepository",
    "TripActivityRepository",
    "TripRepository",
    "UserRepository",
    "WeatherForecastRepository",
]
