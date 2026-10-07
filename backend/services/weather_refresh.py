"""Weather-only refresh for a saved (often reserved) trip.

A booked trip is locked against price/destination re-planning, but its forecast still moves
as the dates approach. This re-fetches **only** the weather for the trip's saved destination
and dates, via the engine's Open-Meteo weather agent, and maps it onto the API
:class:`~schemas.trip_schema.PlanWeatherDay` shape.

Open-Meteo only forecasts ~16 days ahead; further-out windows degrade to seasonal normals
(``estimated``), which isn't a live update — so ``refresh`` reports ``updated=False`` and a
clear message in that case. It also never raises: any failure (engine not installed,
network) degrades to "unavailable", so the lock/snapshot flow is never broken.

The engine is imported lazily inside the method, keeping this module importable in the
stub-only deployment and overridable in tests via the ``get_weather_refresher`` seam.
"""

from __future__ import annotations

import logging
from typing import Protocol, runtime_checkable

from schemas.trip_schema import PlanWeatherDay

logger = logging.getLogger(__name__)


@runtime_checkable
class WeatherRefresher(Protocol):
    """Re-fetch the forecast for a destination+window → (days, updated, message)."""

    def refresh(
        self,
        latitude: float,
        longitude: float,
        label: str,
        start_date: str | None,
        end_date: str | None,
    ) -> tuple[list[PlanWeatherDay], bool, str]: ...


class NullWeatherRefresher:
    """No-op refresher (the default when the engine isn't wired)."""

    def refresh(
        self,
        latitude: float,
        longitude: float,
        label: str,
        start_date: str | None,
        end_date: str | None,
    ) -> tuple[list[PlanWeatherDay], bool, str]:
        return [], False, "Weather refresh is unavailable right now."


class EngineWeatherRefresher:
    """Re-fetches the forecast via the engine's Open-Meteo weather agent (built lazily)."""

    def __init__(self) -> None:
        self._agent = None  # built on first use

    def _weather_agent(self):
        if self._agent is None:
            from ai_engine.agents.weather_agent import WeatherAgent
            from ai_engine.providers.weather import (
                ClimatologyProvider,
                OpenMeteoProvider,
                WeatherService,
                open_meteo_archive_transport,
                open_meteo_http_transport,
            )

            self._agent = WeatherAgent(
                WeatherService(
                    primary=OpenMeteoProvider(open_meteo_http_transport()),
                    climatology=ClimatologyProvider(open_meteo_archive_transport()),
                )
            )
        return self._agent

    def refresh(
        self,
        latitude: float,
        longitude: float,
        label: str,
        start_date: str | None,
        end_date: str | None,
    ) -> tuple[list[PlanWeatherDay], bool, str]:
        try:
            from ai_engine.utils.geo import GeoPoint

            profile = self._weather_agent().profile_for(
                GeoPoint(latitude, longitude),
                destination=label,
                start_date=start_date,
                end_date=end_date,
            )
        except Exception as exc:  # noqa: BLE001 - weather refresh must never break the flow
            logger.warning("weather_refresh: forecast fetch failed for %s (%s)", label, exc)
            return [], False, "Weather refresh is unavailable right now."

        if not profile.days:
            return [], False, "No updated forecast is available for these dates yet."

        days = [
            PlanWeatherDay(
                forecast_date=d.forecast_date,
                temp_min=d.temp_min,
                temp_max=d.temp_max,
                precip_prob=d.precip_probability,
                conditions=d.conditions,
                weather_conflict=d.weather_conflict,
            )
            for d in profile.days
        ]
        # A live/cached forecast is a real update; climatology (estimated) is just the same
        # seasonal typical we already show for far-out trips.
        quality = getattr(profile.quality_flag, "value", str(profile.quality_flag)).lower()
        if quality in ("real", "cached"):
            return days, True, "Weather updated from the latest forecast."
        return (
            days,
            False,
            "Showing typical conditions — a live forecast isn't available for these dates yet.",
        )
