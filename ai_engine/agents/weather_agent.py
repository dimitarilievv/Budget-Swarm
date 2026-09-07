"""WeatherAgent — per-day forecast retrieval per candidate (BS-067).

WeatherAgent answers "what will the weather be during the stay?" for a candidate
city and date window (technical-doc §12). It retrieves the daily forecast through
the :class:`~ai_engine.providers.weather.WeatherService` (Open-Meteo, keyless,
cached with a short TTL because forecasts are volatile — §23) and emits exactly one
normalized domain :class:`~ai_engine.domain.models.WeatherProfile` for the window:
the ordered per-day :class:`~ai_engine.domain.models.WeatherDay` entries plus the
derived ``risk_flags``.

**It does not schedule or replan the itinerary.** That is the optimizer's job
(technical-doc §13): WeatherAgent only *reports* the forecast and flags derived
hazards. Each :class:`~ai_engine.domain.models.WeatherDay` carries a deterministic
``weather_conflict`` flag derived by a **configurable** :class:`WeatherConflictPolicy`
(BS-068) — precipitation probability at/above a tunable threshold, a precipitation
total, or a wet condition label — and this per-day flag is what the coordinator
turns into the OR-Tools weather constraint. The profile-level ``risk_flags`` summarise
these: ``weather_conflict`` when any day conflicts, ``weather_partial`` when some days
were dropped but others survived, and ``weather_unchecked`` when the forecast could
not be obtained at all. The agent never decides which activity happens on which day.

"Degrade, don't crash" (ADR-005): the service already degrades to
**weather-unchecked** (an empty, ``estimated`` result) when Open-Meteo is down with
nothing cached, so the agent emits an empty profile flagged ``estimated`` rather
than failing — the trip proceeds without a weather constraint.

Normalization is the single exit point: the provider's flat per-day shape (date as
a string, a ``weather_conflict`` bool) is mapped onto the domain ``WeatherDay`` /
``WeatherProfile`` value objects, so the coordinator never sees a provider-specific
field. ``ai_engine`` stays free of any backend/ORM import: the forecast source and
its short-TTL cache are injected into the provider (that wiring is the
backend/pipeline's job).
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field, replace
from datetime import date

from ai_engine.domain import QualityFlag, WeatherDay, WeatherProfile
from ai_engine.providers.weather import WeatherProfile as DayForecast
from ai_engine.providers.weather import WeatherRequest, WeatherService
from ai_engine.utils.api_clients import ProviderError, ProviderResult, ProviderUnavailable

logger = logging.getLogger(__name__)

# Derived hazard flags surfaced on the profile for the optimizer/UI to act on.
RISK_WEATHER_CONFLICT = "weather_conflict"
RISK_WEATHER_UNCHECKED = "weather_unchecked"
# Some days were dropped (bad/malformed rows) but others survived — the profile is
# usable but incomplete, so the optimizer/UI knows not to treat it as the full window.
RISK_WEATHER_PARTIAL = "weather_partial"

# Default precipitation-probability (%) at/above which a day is a weather conflict.
DEFAULT_PRECIP_PROBABILITY_THRESHOLD = 60.0
# Open-Meteo serves at most ~16 days of daily forecast.
MAX_FORECAST_DAYS = 16
# Canonical "wet" condition words. Matched against whole, lower-cased tokens of the
# condition label (so "rain" matches "light rain"/"rain showers" but never an
# unrelated substring), covering the WMO labels the provider emits.
DEFAULT_CONFLICT_CONDITIONS = frozenset(
    {"rain", "drizzle", "snow", "sleet", "showers", "thunderstorm", "storm", "hail", "freezing"}
)

_CONDITION_TOKEN_SPLIT = re.compile(r"[^a-z]+")


@dataclass(frozen=True)
class WeatherConflictPolicy:
    """The deterministic rule that decides if a day is a weather conflict (BS-068).

    A day conflicts when **any** configured signal fires (technical-doc §12): its
    precipitation probability is at or above ``precip_probability_threshold``, its
    precipitation total is at or above ``precipitation_mm_threshold`` (when set), or
    its condition label contains one of ``conflict_conditions`` (rain/snow/thunder
    …), matched on whole words rather than raw substrings. The probability threshold
    is the primary, configurable knob; the whole policy is an immutable value so a
    caller can tune it (e.g. a stricter 40% for a rain-averse traveller) without
    touching the agent. The result feeds the OR-Tools weather constraint, so the rule
    is deterministic — no randomness, no I/O.
    """

    precip_probability_threshold: float = DEFAULT_PRECIP_PROBABILITY_THRESHOLD
    precipitation_mm_threshold: float | None = None
    conflict_conditions: frozenset[str] = field(default_factory=lambda: DEFAULT_CONFLICT_CONDITIONS)

    def __post_init__(self) -> None:
        if not 0.0 <= self.precip_probability_threshold <= 100.0:
            raise ValueError(
                "precip_probability_threshold must be a percentage in [0, 100], "
                f"got {self.precip_probability_threshold!r}"
            )
        if self.precipitation_mm_threshold is not None and self.precipitation_mm_threshold < 0:
            raise ValueError(
                "precipitation_mm_threshold must be non-negative, "
                f"got {self.precipitation_mm_threshold!r}"
            )

    def is_conflict(self, day: WeatherDay) -> bool:
        """True when ``day`` is unfit for outdoor activities under this policy."""
        if (
            day.precip_probability is not None
            and day.precip_probability >= self.precip_probability_threshold
        ):
            return True
        if (
            self.precipitation_mm_threshold is not None
            and day.precipitation_mm is not None
            and day.precipitation_mm >= self.precipitation_mm_threshold
        ):
            return True
        tokens = {t for t in _CONDITION_TOKEN_SPLIT.split((day.conditions or "").lower()) if t}
        return bool(tokens & self.conflict_conditions)


class WeatherAgent:
    """Retrieve and normalize the per-day forecast for a candidate (technical-doc §12).

    Constructed with a :class:`WeatherService` (Open-Meteo with a weather-unchecked
    degrade). :meth:`profile_for` returns one :class:`WeatherProfile` for a
    destination and date window. It performs no itinerary scheduling.
    """

    def __init__(
        self, weather: WeatherService, *, conflict_policy: WeatherConflictPolicy | None = None
    ) -> None:
        self.weather = weather
        self.conflict_policy = conflict_policy or WeatherConflictPolicy()

    def profile_for(
        self,
        center,
        *,
        destination: str | None = None,
        start_date: str | None = None,
        end_date: str | None = None,
        forecast_days: int = 7,
    ) -> WeatherProfile:
        """Return the normalized :class:`WeatherProfile` for a candidate + window.

        Resolves the forecast from the :class:`WeatherService` (``real`` live,
        ``cached`` from the short-TTL cache, ``estimated`` when weather-unchecked),
        maps each day onto a :class:`WeatherDay`, and derives ``risk_flags``. Never
        raises: a missing forecast degrades to an empty, ``estimated`` profile
        flagged ``weather_unchecked``.
        """
        _validate_window(start_date, end_date, forecast_days)
        request = WeatherRequest(
            center=center,
            destination=destination,
            start_date=start_date,
            end_date=end_date,
            forecast_days=forecast_days,
        )

        try:
            result: ProviderResult[list[DayForecast]] = self.weather.resolve(request)
        except ProviderError as exc:
            # WeatherService normally degrades internally; guard anyway (ADR-005).
            reason = "unreachable" if isinstance(exc, ProviderUnavailable) else "no data"
            logger.warning(
                "weather: forecast %s for %s (%s); weather-unchecked",
                reason,
                destination or center,
                exc,
            )
            return WeatherProfile(
                days=(), risk_flags=(RISK_WEATHER_UNCHECKED,), quality_flag=QualityFlag.ESTIMATED
            )

        quality = QualityFlag(result.quality_flag)
        forecasts = result.data or []
        mapped = [self._to_day(forecast) for forecast in forecasts]
        # Sort explicitly by date — never rely on the provider's ordering — and count
        # how many rows were dropped so a partial window is flagged, not hidden.
        days = tuple(sorted((d for d in mapped if d is not None), key=lambda d: d.forecast_date))
        skipped = len(forecasts) - len(days)
        risk_flags = self._risk_flags(days, skipped=skipped)
        logger.debug(
            "weather: %s -> %d day(s) (%d skipped), risks=%s [%s]",
            destination or center,
            len(days),
            skipped,
            risk_flags or "none",
            quality,
        )
        return WeatherProfile(days=days, risk_flags=risk_flags, quality_flag=quality)

    def _to_day(self, forecast: DayForecast) -> WeatherDay | None:
        """Map one provider per-day forecast onto a domain :class:`WeatherDay`.

        The per-day ``weather_conflict`` flag is (re-)derived here with this agent's
        configurable :class:`WeatherConflictPolicy` (BS-068) rather than trusting the
        provider's fixed-threshold bool, so the threshold is tunable. A row with an
        unparseable date is skipped (logged) rather than aborting the whole profile.
        """
        try:
            forecast_date = date.fromisoformat(forecast.date)
        except (ValueError, TypeError):
            logger.info("weather: skipping day with bad date %r", forecast.date)
            return None
        day = WeatherDay(
            forecast_date=forecast_date,
            temp_min=forecast.temp_min,
            temp_max=forecast.temp_max,
            precipitation_mm=forecast.precipitation_mm,
            precip_probability=forecast.precip_prob,
            wind_speed=forecast.wind_speed,
            conditions=forecast.conditions,
        )
        return replace(day, weather_conflict=self.conflict_policy.is_conflict(day))

    @staticmethod
    def _risk_flags(days: tuple[WeatherDay, ...], *, skipped: int = 0) -> tuple[str, ...]:
        """Derive profile-level hazard flags from the per-day conflict flags.

        Reporting only — no itinerary decisions. ``weather_unchecked`` when there is
        no usable forecast at all; otherwise ``weather_conflict`` when any day
        conflicts and ``weather_partial`` when some days were dropped but others
        survived (an incomplete but usable window).
        """
        if not days:
            return (RISK_WEATHER_UNCHECKED,)
        flags: list[str] = []
        if any(day.weather_conflict for day in days):
            flags.append(RISK_WEATHER_CONFLICT)
        if skipped > 0:
            flags.append(RISK_WEATHER_PARTIAL)
        return tuple(flags)


def _validate_window(start_date: str | None, end_date: str | None, forecast_days: int) -> None:
    """Validate the forecast window: an explicit ISO date range, or forecast_days.

    When either of ``start_date`` / ``end_date`` is given, both must be valid
    ``YYYY-MM-DD`` dates with ``start <= end``; otherwise ``forecast_days`` must be
    within ``[1, MAX_FORECAST_DAYS]``.
    """
    if start_date is not None or end_date is not None:
        if start_date is None or end_date is None:
            raise ValueError("start_date and end_date must be given together")
        try:
            start = date.fromisoformat(start_date)
            end = date.fromisoformat(end_date)
        except ValueError as exc:
            raise ValueError(f"start_date/end_date must be YYYY-MM-DD: {exc}") from exc
        if start > end:
            raise ValueError(f"start_date ({start_date}) must not be after end_date ({end_date})")
        return
    if not 1 <= forecast_days <= MAX_FORECAST_DAYS:
        raise ValueError(
            f"forecast_days must be within [1, {MAX_FORECAST_DAYS}], got {forecast_days!r}"
        )
