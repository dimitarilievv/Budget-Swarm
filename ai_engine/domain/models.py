"""The five internal domain models (BS-038).

Each is a frozen, validated dataclass. They are intentionally small and
provider-agnostic: only the normalized fields the coordinator/scorer/optimizer
actually need appear here, and nothing that names or reveals a specific provider
(no listing ids, vendor URLs, raw payloads, or source-specific codes).

Money is carried as ``float`` EUR (all provider prices are converted to a common
EUR basis during normalization — technical-doc §19). ``None`` means "unknown", not
"zero": a missing price is left as ``None`` and typically pushes ``quality_flag``
toward ``estimated`` upstream, rather than being silently defaulted.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date

from .enums import IndoorOutdoor, QualityFlag, TransportMode

# --- shared field validators -------------------------------------------------
#
# Small pure helpers so every model validates the same way. They raise
# ``ValueError`` (with a field-qualified message) on bad input, which is what the
# construction/validation unit tests assert against.


def _check_latitude(name: str, value: float | None) -> None:
    if value is not None and not (-90.0 <= value <= 90.0):
        raise ValueError(f"{name} must be within [-90, 90], got {value!r}")


def _check_longitude(name: str, value: float | None) -> None:
    if value is not None and not (-180.0 <= value <= 180.0):
        raise ValueError(f"{name} must be within [-180, 180], got {value!r}")


def _check_non_negative(name: str, value: float | int | None) -> None:
    if value is not None and value < 0:
        raise ValueError(f"{name} must be non-negative, got {value!r}")


def _check_non_empty(name: str, value: str) -> None:
    if not value or not value.strip():
        raise ValueError(f"{name} must be a non-empty string")


def _coerce_enum(name: str, value: object, enum_cls: type) -> object:
    """Accept either an enum member or its raw string value; reject anything else."""
    if isinstance(value, enum_cls):
        return value
    try:
        return enum_cls(value)
    except ValueError as exc:
        allowed = ", ".join(repr(m.value) for m in enum_cls)
        raise ValueError(f"{name} must be one of {allowed}, got {value!r}") from exc


# --- 1. Destination ----------------------------------------------------------


@dataclass(frozen=True)
class DestinationFeatures:
    """Normalized 0–1 preference scores + cost index for a destination.

    Mirrors the ``destination_features`` feature store (technical-doc §19/§20). All
    scores are unit-normalized to ``[0, 1]``; ``cost_of_living`` is an index where
    100 is the reference basket (so values above/below 100 are dearer/cheaper).
    Every field is optional because a freshly added city may not be enriched yet.
    """

    cost_of_living: float | None = None
    tourism_score: float | None = None
    culture_score: float | None = None
    nature_score: float | None = None
    nightlife_score: float | None = None
    food_score: float | None = None
    season_score: float | None = None

    def __post_init__(self) -> None:
        _check_non_negative("cost_of_living", self.cost_of_living)
        for name in (
            "tourism_score",
            "culture_score",
            "nature_score",
            "nightlife_score",
            "food_score",
            "season_score",
        ):
            value = getattr(self, name)
            if value is not None and not (0.0 <= value <= 1.0):
                raise ValueError(f"{name} must be within [0, 1], got {value!r}")


@dataclass(frozen=True)
class Destination:
    """A candidate city produced by ScoutAgent and consumed by the coordinator.

    Carries only identity + coordinates + the normalized feature scores the scorer
    needs. ``quality_flag`` reflects how firm the feature data behind this
    candidate is (curated/real vs. an estimated fallback).
    """

    city: str
    country: str
    latitude: float
    longitude: float
    quality_flag: QualityFlag = QualityFlag.REAL
    country_code: str | None = None
    features: DestinationFeatures | None = None

    def __post_init__(self) -> None:
        _check_non_empty("city", self.city)
        _check_non_empty("country", self.country)
        _check_latitude("latitude", self.latitude)
        _check_longitude("longitude", self.longitude)
        if self.country_code is not None and len(self.country_code) != 2:
            raise ValueError(f"country_code must be a 2-letter code, got {self.country_code!r}")
        object.__setattr__(
            self, "quality_flag", _coerce_enum("quality_flag", self.quality_flag, QualityFlag)
        )


# --- 2. TransportOption ------------------------------------------------------


@dataclass(frozen=True)
class TransportOption:
    """One costed way to reach a destination, for a single mode (TransportAgent).

    A Travelpayouts flight record, an OpenRouteService car route and a scraped
    bus/train fare all normalize to this same shape (technical-doc §19). ``cost`` is
    EUR; ``route_summary`` is a short human-readable description of the route
    (e.g. "SKP → MUC, 1 stop") — never a provider-specific payload.
    """

    mode: TransportMode
    cost: float | None = None
    duration_minutes: int | None = None
    distance_km: float | None = None
    route_summary: str | None = None
    quality_flag: QualityFlag = QualityFlag.REAL
    # Indicative outbound departure timestamp for this option, when the source provides
    # one (e.g. the Travelpayouts cheapest-fare ``departure_at``). ISO-8601 string —
    # a date, or a date-time; ``None`` for modes/sources without a scheduled departure.
    departure_at: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "mode", _coerce_enum("mode", self.mode, TransportMode))
        _check_non_negative("cost", self.cost)
        _check_non_negative("duration_minutes", self.duration_minutes)
        _check_non_negative("distance_km", self.distance_km)
        object.__setattr__(
            self, "quality_flag", _coerce_enum("quality_flag", self.quality_flag, QualityFlag)
        )


# --- 3. Accommodation --------------------------------------------------------


@dataclass(frozen=True)
class Accommodation:
    """One normalized lodging option at a destination (StayAgent).

    Sourced from Inside Airbnb (or a per-city price-statistics estimate when a city
    has no listings), but nothing here reveals that: ``accommodation_type`` is a
    plain category string, prices are EUR per night. ``rating`` is on a 0–5 scale.
    """

    price_per_night: float
    quality_flag: QualityFlag = QualityFlag.REAL
    name: str | None = None
    accommodation_type: str | None = None
    rating: float | None = None
    latitude: float | None = None
    longitude: float | None = None
    # Optional listing detail for the UI card (BS-104); present only for real listings,
    # ``None``/empty for a price-stats-estimate option.
    listing_url: str | None = None
    picture_url: str | None = None
    review_count: int | None = None
    location_score: float | None = None
    neighbourhood: str | None = None
    amenities: tuple[str, ...] = ()
    accommodates: int | None = None  # guest capacity (Inside Airbnb)

    def __post_init__(self) -> None:
        if self.price_per_night is None:
            raise ValueError("price_per_night is required")
        _check_non_negative("price_per_night", self.price_per_night)
        if self.rating is not None and not (0.0 <= self.rating <= 5.0):
            raise ValueError(f"rating must be within [0, 5], got {self.rating!r}")
        _check_latitude("latitude", self.latitude)
        _check_longitude("longitude", self.longitude)
        # Freeze amenities to a tuple so the value object stays immutable/hashable.
        object.__setattr__(self, "amenities", tuple(self.amenities or ()))
        object.__setattr__(
            self, "quality_flag", _coerce_enum("quality_flag", self.quality_flag, QualityFlag)
        )


# --- 4. Activity -------------------------------------------------------------


@dataclass(frozen=True)
class Activity:
    """One thing to do at a destination (ExperienceAgent).

    Normalized from OpenTripMap / OpenStreetMap POIs. ``indoor_outdoor`` lets the
    optimizer weigh an activity against the :class:`WeatherProfile`; ``cost`` is an
    optional EUR entrance price (``None`` = free/unknown). ``description`` is an
    optional short summary from enrichment (e.g. Wikipedia), source-agnostic and
    ``None`` when the activity was not enriched.
    """

    name: str
    category: str | None = None
    latitude: float | None = None
    longitude: float | None = None
    opening_hours: str | None = None
    indoor_outdoor: IndoorOutdoor = IndoorOutdoor.MIXED
    est_duration_minutes: int | None = None
    cost: float | None = None
    description: str | None = None
    quality_flag: QualityFlag = QualityFlag.REAL

    def __post_init__(self) -> None:
        _check_non_empty("name", self.name)
        _check_latitude("latitude", self.latitude)
        _check_longitude("longitude", self.longitude)
        _check_non_negative("est_duration_minutes", self.est_duration_minutes)
        _check_non_negative("cost", self.cost)
        object.__setattr__(
            self,
            "indoor_outdoor",
            _coerce_enum("indoor_outdoor", self.indoor_outdoor, IndoorOutdoor),
        )
        object.__setattr__(
            self, "quality_flag", _coerce_enum("quality_flag", self.quality_flag, QualityFlag)
        )


# --- 5. WeatherProfile -------------------------------------------------------


@dataclass(frozen=True)
class WeatherDay:
    """The forecast for a single day of a stay (one entry of a WeatherProfile)."""

    forecast_date: date
    temp_min: float | None = None
    temp_max: float | None = None
    precipitation_mm: float | None = None
    precip_probability: float | None = None
    wind_speed: float | None = None
    conditions: str | None = None
    # Deterministic weather-conflict flag (WeatherAgent, BS-068): True when the day
    # is unfit for outdoor activities. Exposed to the coordinator/optimizer, which
    # turns it into the OR-Tools weather constraint (technical-doc §12/§13).
    weather_conflict: bool = False

    def __post_init__(self) -> None:
        if not isinstance(self.forecast_date, date):
            raise ValueError("forecast_date must be a datetime.date")
        if (
            self.temp_min is not None
            and self.temp_max is not None
            and self.temp_min > self.temp_max
        ):
            raise ValueError(
                f"temp_min ({self.temp_min}) must not exceed temp_max ({self.temp_max})"
            )
        _check_non_negative("precipitation_mm", self.precipitation_mm)
        _check_non_negative("wind_speed", self.wind_speed)
        if self.precip_probability is not None and not (0.0 <= self.precip_probability <= 100.0):
            raise ValueError(
                f"precip_probability must be a percentage in [0, 100], "
                f"got {self.precip_probability!r}"
            )


@dataclass(frozen=True)
class WeatherProfile:
    """The per-day forecast for a destination over the stay window (WeatherAgent).

    Normalized from Open-Meteo. ``days`` is ordered by date; ``risk_flags`` names
    derived hazards (e.g. ``"weather_conflict"`` when outdoor activities clash with
    rain) that the optimizer/UI surface without re-deriving them.
    """

    days: tuple[WeatherDay, ...] = ()
    risk_flags: tuple[str, ...] = ()
    quality_flag: QualityFlag = QualityFlag.REAL

    def __post_init__(self) -> None:
        # Accept any sequence at the boundary but freeze to a tuple so the value
        # object stays immutable and hashable.
        object.__setattr__(self, "days", tuple(self.days))
        object.__setattr__(self, "risk_flags", tuple(self.risk_flags))
        for i, day in enumerate(self.days):
            if not isinstance(day, WeatherDay):
                raise ValueError(f"days[{i}] must be a WeatherDay, got {type(day).__name__}")
        object.__setattr__(
            self, "quality_flag", _coerce_enum("quality_flag", self.quality_flag, QualityFlag)
        )
