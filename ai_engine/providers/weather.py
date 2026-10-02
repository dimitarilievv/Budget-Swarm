"""WeatherProvider + Open-Meteo adapter (BS-052; §11/§22).

Weather for the travel window comes from **Open-Meteo** (verified BS-009): fully
free, **no key**, generous limits. This provider normalizes its daily forecast
into one :class:`WeatherProfile` per day (temps, precipitation, wind, a WMO
condition label, and a derived ``weather_conflict`` flag that marks days unfit for
outdoor activities — the interface to WeatherAgent's replanning, §11.3/§12).
Forecasts are volatile, so they are cached with a **short TTL** (hours, §23),
keyed per destination + date range.

Graceful degrade (§22): if Open-Meteo is unavailable, a fresh-enough cached entry
is served (handled by the BS-037 read-through under the base provider); with no
cache to fall back on, :class:`WeatherService` degrades to **weather-unchecked** —
an empty profile list flagged ``estimated`` — so the trip proceeds without a
weather constraint rather than failing (the "degrade, don't crash" rule).

The HTTP call is injected so the adapter is unit-tested against a recorded
response with no network.
"""

from __future__ import annotations

import logging
import statistics
from abc import ABC
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from typing import Any

from ai_engine.utils.api_clients import (
    QUALITY_ESTIMATED,
    BaseProvider,
    ProviderNoResult,
    ProviderResult,
    ProviderUnavailable,
)
from ai_engine.utils.geo import GeoPoint

logger = logging.getLogger(__name__)

FORECAST_URL = "https://api.open-meteo.com/v1/forecast"
ARCHIVE_URL = "https://archive-api.open-meteo.com/v1/archive"
WEATHER_UNCHECKED = "weather-unchecked"
# How many recent years of history to average into the climatology ("typical conditions")
# fallback, used when a trip is further out than the live forecast horizon.
CLIMATOLOGY_YEARS = 5

# WMO weather-interpretation codes → short condition labels (Open-Meteo `weathercode`).
_WMO_CONDITIONS: dict[int, str] = {
    0: "clear",
    1: "mainly clear",
    2: "partly cloudy",
    3: "overcast",
    45: "fog",
    48: "rime fog",
    51: "light drizzle",
    53: "drizzle",
    55: "dense drizzle",
    56: "freezing drizzle",
    57: "freezing drizzle",
    61: "light rain",
    63: "rain",
    65: "heavy rain",
    66: "freezing rain",
    67: "freezing rain",
    71: "light snow",
    73: "snow",
    75: "heavy snow",
    77: "snow grains",
    80: "rain showers",
    81: "rain showers",
    82: "violent rain showers",
    85: "snow showers",
    86: "snow showers",
    95: "thunderstorm",
    96: "thunderstorm with hail",
    99: "thunderstorm with hail",
}
# Codes that make a day unsuitable for outdoor activities (rain/snow/thunder).
_CONFLICT_CODES = frozenset(
    {51, 53, 55, 56, 57, 61, 63, 65, 66, 67, 71, 73, 75, 77, 80, 81, 82, 85, 86, 95, 96, 99}
)
# Precipitation-probability (%) at/above which a day is a conflict regardless of code.
_CONFLICT_PRECIP_PROB = 60.0


def wmo_condition(code: int | None) -> str:
    return _WMO_CONDITIONS.get(code, "unknown") if code is not None else "unknown"


def is_weather_conflict(code: int | None, precip_prob: float | None) -> bool:
    """A day is a conflict if its code is rain/snow/thunder or rain is likely."""
    if code is not None and code in _CONFLICT_CODES:
        return True
    return precip_prob is not None and precip_prob >= _CONFLICT_PRECIP_PROB


@dataclass(frozen=True)
class WeatherRequest:
    """A forecast query around a destination centre for a date window."""

    center: GeoPoint
    destination: str | None = None
    start_date: str | None = None  # YYYY-MM-DD; with end_date, an explicit window
    end_date: str | None = None
    forecast_days: int = 7  # used when start/end are not given


@dataclass
class WeatherProfile:
    """Normalized single-day forecast (mirrors the weather_forecasts columns)."""

    date: str
    temp_min: float | None
    temp_max: float | None
    precipitation_mm: float | None
    precip_prob: float | None
    wind_speed: float | None
    conditions: str
    weather_conflict: bool


WeatherTransport = Callable[[WeatherRequest], dict[str, Any]]


class WeatherProvider(BaseProvider[WeatherRequest, list[WeatherProfile]], ABC):
    """Interface for a source of normalized daily forecasts."""

    name = "weather"
    cache_type = "weather"  # short TTL — forecasts move within hours (§23)

    def cache_key(self, request: WeatherRequest) -> dict[str, Any]:
        """Cache per destination + date window (short TTL set by the caller)."""
        return {
            "dest": request.destination
            or [round(request.center.lat, 4), round(request.center.lon, 4)],
            "start": request.start_date,
            "end": request.end_date,
            "days": None if request.start_date else request.forecast_days,
        }


class OpenMeteoProvider(WeatherProvider):
    """Primary (only needed) weather provider — Open-Meteo, keyless (BS-009)."""

    name = "open-meteo"

    def __init__(self, transport: WeatherTransport, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self._transport = transport

    def _raw(self, request: WeatherRequest) -> dict[str, Any]:
        try:
            return self._transport(request)
        except ProviderUnavailable:
            raise
        except Exception as exc:  # noqa: BLE001
            raise ProviderUnavailable(f"Open-Meteo request failed: {exc}") from exc

    def _from_raw(self, request: WeatherRequest, raw: object) -> list[WeatherProfile]:
        return self._normalize(raw)  # type: ignore[arg-type]

    @staticmethod
    def _normalize(body: dict[str, Any]) -> list[WeatherProfile]:
        daily = (body or {}).get("daily") or {}
        times = daily.get("time") or []
        if not times:
            raise ProviderNoResult("Open-Meteo returned no daily forecast")
        tmax = daily.get("temperature_2m_max") or []
        tmin = daily.get("temperature_2m_min") or []
        precip = daily.get("precipitation_sum") or []
        prob = daily.get("precipitation_probability_max") or []
        wind = daily.get("windspeed_10m_max") or []
        codes = daily.get("weathercode") or []

        def at(seq: list, i: int) -> Any:
            return seq[i] if i < len(seq) else None

        profiles: list[WeatherProfile] = []
        for i, day in enumerate(times):
            code = at(codes, i)
            precip_prob = at(prob, i)
            profiles.append(
                WeatherProfile(
                    date=day,
                    temp_min=at(tmin, i),
                    temp_max=at(tmax, i),
                    precipitation_mm=at(precip, i),
                    precip_prob=precip_prob,
                    wind_speed=at(wind, i),
                    conditions=wmo_condition(code),
                    weather_conflict=is_weather_conflict(code, precip_prob),
                )
            )
        return profiles


class ClimatologyProvider(WeatherProvider):
    """ "Typical conditions" for a future window, averaged from recent years' observations.

    The live forecast only reaches ~16 days out, so a trip planned further ahead would show no
    weather at all. This provider fills that gap honestly: it reads the **Open-Meteo historical
    archive** (real past observations, keyless) for the window's calendar days across the last
    :data:`CLIMATOLOGY_YEARS` years and averages them into one profile per trip date — a
    climatology, not a forecast. It is flagged ``estimated`` and the UI discloses that actual
    weather this far ahead can differ. Only used for an explicit future date window.
    """

    name = "climatology"

    def __init__(
        self, transport: WeatherTransport, *, years: int = CLIMATOLOGY_YEARS, **kwargs: Any
    ) -> None:
        super().__init__(**kwargs)
        self._transport = transport
        self._years = years

    def _raw(self, request: WeatherRequest) -> dict[str, Any]:
        try:
            return self._transport(request)
        except (ProviderUnavailable, ProviderNoResult):
            raise
        except Exception as exc:  # noqa: BLE001
            raise ProviderUnavailable(f"Open-Meteo archive request failed: {exc}") from exc

    def _from_raw(self, request: WeatherRequest, raw: object) -> list[WeatherProfile]:
        return self._average(request, raw)  # type: ignore[arg-type]

    @staticmethod
    def _average(request: WeatherRequest, body: dict[str, Any]) -> list[WeatherProfile]:
        if not request.start_date or not request.end_date:
            raise ProviderNoResult("climatology needs an explicit date window")
        daily = (body or {}).get("daily") or {}
        times = daily.get("time") or []
        if not times:
            raise ProviderNoResult("Open-Meteo archive returned no history")
        tmax = daily.get("temperature_2m_max") or []
        tmin = daily.get("temperature_2m_min") or []
        precip = daily.get("precipitation_sum") or []
        wind = daily.get("windspeed_10m_max") or []
        codes = daily.get("weathercode") or []

        # Group each historical day by its (month, day) so we can average over the years.
        by_md: dict[tuple[int, int], list[dict[str, Any]]] = {}
        for i, iso in enumerate(times):
            try:
                hd = date.fromisoformat(iso)
            except (ValueError, TypeError):
                continue
            by_md.setdefault((hd.month, hd.day), []).append(
                {
                    "tmax": _at(tmax, i),
                    "tmin": _at(tmin, i),
                    "precip": _at(precip, i),
                    "wind": _at(wind, i),
                    "code": _at(codes, i),
                }
            )

        start = date.fromisoformat(request.start_date)
        end = date.fromisoformat(request.end_date)
        profiles: list[WeatherProfile] = []
        day = start
        while day <= end:
            samples = by_md.get((day.month, day.day), [])
            profile = _climatology_day(day, samples)
            if profile is not None:
                profiles.append(profile)
            day += timedelta(days=1)
        if not profiles:
            raise ProviderNoResult("no overlapping history for the requested window")
        return profiles


def _at(seq: list, i: int) -> Any:
    return seq[i] if i < len(seq) else None


def _mean(values: list[Any]) -> float | None:
    nums = [float(v) for v in values if isinstance(v, int | float)]
    return round(statistics.fmean(nums), 1) if nums else None


def _climatology_day(day: date, samples: list[dict[str, Any]]) -> WeatherProfile | None:
    """Average a day's historical samples into a typical-conditions profile (``None`` if empty)."""
    if not samples:
        return None
    precip_vals = [float(s["precip"]) for s in samples if isinstance(s["precip"], int | float)]
    # A real, observed "rain chance": the share of past years that saw measurable rain this day.
    precip_prob: float | None = None
    if precip_vals:
        precip_prob = round(sum(1 for p in precip_vals if p >= 1.0) / len(precip_vals) * 100)
    code_vals = [int(s["code"]) for s in samples if isinstance(s["code"], int | float)]
    modal_code = Counter(code_vals).most_common(1)[0][0] if code_vals else None
    return WeatherProfile(
        date=day.isoformat(),
        temp_min=_mean([s["tmin"] for s in samples]),
        temp_max=_mean([s["tmax"] for s in samples]),
        precipitation_mm=_mean([s["precip"] for s in samples]),
        precip_prob=precip_prob,
        wind_speed=_mean([s["wind"] for s in samples]),
        conditions=wmo_condition(modal_code),
        weather_conflict=is_weather_conflict(modal_code, precip_prob),
    )


@dataclass
class WeatherService:
    """Open-Meteo forecast → climatology → weather-unchecked degrade (§22).

    ``resolve(request)`` returns the live forecast (``real``/``cached``). For a future window
    the forecast can't reach (further out than ~16 days), it falls back to the optional
    ``climatology`` provider — typical conditions averaged from recent years, flagged
    ``estimated``. If neither is available, it degrades to **weather-unchecked**: an empty
    profile list flagged ``estimated``, so the caller proceeds without a weather constraint.
    """

    primary: WeatherProvider
    climatology: WeatherProvider | None = None

    def resolve(self, request: WeatherRequest) -> ProviderResult[list[WeatherProfile]]:
        try:
            return self.primary.fetch(request)
        except (ProviderUnavailable, ProviderNoResult) as exc:
            if self.climatology is not None and request.start_date and request.end_date:
                try:
                    climo = self.climatology.fetch(request)
                    if climo.data:
                        logger.info(
                            "weather: live forecast out of range; using %d-day climatology",
                            len(climo.data),
                        )
                        return ProviderResult(
                            climo.data, QUALITY_ESTIMATED, self.climatology.name, "estimate"
                        )
                except (ProviderUnavailable, ProviderNoResult) as climo_exc:
                    logger.info("weather: climatology unavailable too (%s)", climo_exc)
            logger.warning(
                "weather: %s unavailable (%s); proceeding weather-unchecked", self.primary.name, exc
            )
            return ProviderResult([], QUALITY_ESTIMATED, WEATHER_UNCHECKED, "estimate")


def open_meteo_http_transport(
    *,
    base_url: str = FORECAST_URL,
    timeout: float = 10.0,
    client: Any | None = None,
) -> WeatherTransport:
    """Build a :data:`WeatherTransport` for the Open-Meteo forecast endpoint (no key).

    Requests the daily fields BudgetSwarm normalizes; 429/5xx → :class:`ProviderUnavailable`.
    """
    import httpx

    def transport(request: WeatherRequest) -> dict[str, Any]:
        owns = client is None
        http = client or httpx.Client(timeout=timeout)
        try:
            params: dict[str, Any] = {
                "latitude": request.center.lat,
                "longitude": request.center.lon,
                "daily": (
                    "temperature_2m_max,temperature_2m_min,precipitation_sum,"
                    "precipitation_probability_max,windspeed_10m_max,weathercode"
                ),
                "timezone": "auto",
            }
            if request.start_date and request.end_date:
                params["start_date"] = request.start_date
                params["end_date"] = request.end_date
            else:
                params["forecast_days"] = request.forecast_days
            resp = http.get(base_url, params=params)
            if resp.status_code == 429 or resp.status_code >= 500:
                raise ProviderUnavailable(f"Open-Meteo HTTP {resp.status_code}")
            resp.raise_for_status()
            return resp.json()
        except httpx.HTTPError as exc:
            raise ProviderUnavailable(f"Open-Meteo transport error: {exc}") from exc
        finally:
            if owns:
                http.close()

    return transport


def open_meteo_archive_transport(
    *,
    base_url: str = ARCHIVE_URL,
    timeout: float = 15.0,
    client: Any | None = None,
    years: int = CLIMATOLOGY_YEARS,
    now: Callable[[], date] | None = None,
) -> WeatherTransport:
    """Build a :data:`WeatherTransport` for the Open-Meteo **historical archive** (no key).

    Given a future date window, it fetches the same calendar span across the last ``years``
    years in one request, so the :class:`ClimatologyProvider` can average it into typical
    conditions. ``now`` is injectable for deterministic tests.
    """
    import httpx

    today = now or (lambda: datetime.now(UTC).date())

    def transport(request: WeatherRequest) -> dict[str, Any]:
        if not (request.start_date and request.end_date):
            raise ProviderNoResult("climatology needs an explicit date window")
        s = date.fromisoformat(request.start_date)
        e = date.fromisoformat(request.end_date)
        this_year = today().year
        # One contiguous historical range covering the window's calendar days across recent
        # years; the provider filters it back down to the window's month-days and averages.
        start_h = date(this_year - years, s.month, s.day)
        end_h = date(this_year - 1, e.month, e.day)
        owns = client is None
        http = client or httpx.Client(timeout=timeout)
        try:
            params: dict[str, Any] = {
                "latitude": request.center.lat,
                "longitude": request.center.lon,
                "start_date": start_h.isoformat(),
                "end_date": end_h.isoformat(),
                "daily": (
                    "temperature_2m_max,temperature_2m_min,precipitation_sum,"
                    "windspeed_10m_max,weathercode"
                ),
                "timezone": "auto",
            }
            resp = http.get(base_url, params=params)
            if resp.status_code == 429 or resp.status_code >= 500:
                raise ProviderUnavailable(f"Open-Meteo archive HTTP {resp.status_code}")
            resp.raise_for_status()
            return resp.json()
        except httpx.HTTPError as exc:
            raise ProviderUnavailable(f"Open-Meteo archive transport error: {exc}") from exc
        finally:
            if owns:
                http.close()

    return transport
