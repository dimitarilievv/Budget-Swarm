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
hazards — ``weather_conflict`` when any day is unfit for outdoor activities (the
interface the optimizer later uses to down-weight outdoor plans), and
``weather_unchecked`` when the forecast could not be obtained at all. It never
decides which activity happens on which day.

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
from datetime import date

from ai_engine.domain import QualityFlag, WeatherDay, WeatherProfile
from ai_engine.providers.weather import WeatherProfile as DayForecast
from ai_engine.providers.weather import WeatherRequest, WeatherService
from ai_engine.utils.api_clients import ProviderError, ProviderResult

logger = logging.getLogger(__name__)

# Derived hazard flags surfaced on the profile for the optimizer/UI to act on.
RISK_WEATHER_CONFLICT = "weather_conflict"
RISK_WEATHER_UNCHECKED = "weather_unchecked"


class WeatherAgent:
    """Retrieve and normalize the per-day forecast for a candidate (technical-doc §12).

    Constructed with a :class:`WeatherService` (Open-Meteo with a weather-unchecked
    degrade). :meth:`profile_for` returns one :class:`WeatherProfile` for a
    destination and date window. It performs no itinerary scheduling.
    """

    def __init__(self, weather: WeatherService) -> None:
        self.weather = weather

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
            logger.warning(
                "weather: forecast unavailable for %s (%s); weather-unchecked",
                destination or center,
                exc,
            )
            return WeatherProfile(
                days=(), risk_flags=(RISK_WEATHER_UNCHECKED,), quality_flag=QualityFlag.ESTIMATED
            )

        quality = QualityFlag(result.quality_flag)
        days = tuple(self._to_day(forecast) for forecast in (result.data or []))
        days = tuple(day for day in days if day is not None)
        risk_flags = self._risk_flags(result.data or [], days)
        logger.debug(
            "weather: %s -> %d day(s), risks=%s [%s]",
            destination or center,
            len(days),
            risk_flags or "none",
            quality,
        )
        return WeatherProfile(days=days, risk_flags=risk_flags, quality_flag=quality)

    @staticmethod
    def _to_day(forecast: DayForecast) -> WeatherDay | None:
        """Map one provider per-day forecast onto a domain :class:`WeatherDay`.

        A row with an unparseable date is skipped (logged) rather than aborting the
        whole profile.
        """
        try:
            forecast_date = date.fromisoformat(forecast.date)
        except (ValueError, TypeError):
            logger.info("weather: skipping day with bad date %r", forecast.date)
            return None
        return WeatherDay(
            forecast_date=forecast_date,
            temp_min=forecast.temp_min,
            temp_max=forecast.temp_max,
            precipitation_mm=forecast.precipitation_mm,
            precip_probability=forecast.precip_prob,
            wind_speed=forecast.wind_speed,
            conditions=forecast.conditions,
        )

    @staticmethod
    def _risk_flags(forecasts: list[DayForecast], days: tuple[WeatherDay, ...]) -> tuple[str, ...]:
        """Derive hazard flags (reporting only — no itinerary decisions).

        ``weather_unchecked`` when there is no forecast at all; otherwise
        ``weather_conflict`` when any day is unfit for outdoor activities.
        """
        if not days:
            return (RISK_WEATHER_UNCHECKED,)
        if any(getattr(f, "weather_conflict", False) for f in forecasts):
            return (RISK_WEATHER_CONFLICT,)
        return ()
