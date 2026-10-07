"""Unit tests for the weather-only refresher (no DB, no network).

The engine refresher is exercised with a fake weather agent injected, so the mapping
(engine ``WeatherDay`` → API ``PlanWeatherDay``) and the live-vs-estimated "updated" rule
are checked without hitting Open-Meteo.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date

from services.weather_refresh import EngineWeatherRefresher, NullWeatherRefresher


@dataclass
class _FakeDay:
    forecast_date: date
    temp_min: float | None = None
    temp_max: float | None = None
    precip_probability: float | None = None
    conditions: str | None = None
    weather_conflict: bool = False


@dataclass
class _FakeQuality:
    value: str


@dataclass
class _FakeProfile:
    days: tuple
    quality_flag: _FakeQuality


class _FakeAgent:
    def __init__(self, profile: _FakeProfile) -> None:
        self._profile = profile

    def profile_for(self, center, *, destination=None, start_date=None, end_date=None):
        return self._profile


def _refresher_with(profile: _FakeProfile) -> EngineWeatherRefresher:
    r = EngineWeatherRefresher()
    r._agent = _FakeAgent(profile)  # inject, bypassing the real engine build
    return r


def test_null_refresher_reports_unavailable():
    days, updated, msg = NullWeatherRefresher().refresh(1.0, 2.0, "X", None, None)
    assert days == [] and updated is False and msg


def test_live_forecast_maps_and_flags_updated():
    profile = _FakeProfile(
        days=(
            _FakeDay(
                forecast_date=date(2026, 11, 1),
                temp_min=5.0,
                temp_max=12.0,
                precip_probability=20.0,
                conditions="Cloudy",
                weather_conflict=False,
            ),
        ),
        quality_flag=_FakeQuality("real"),
    )
    days, updated, _ = _refresher_with(profile).refresh(1.0, 2.0, "X", "2026-11-01", "2026-11-02")
    assert updated is True
    assert days[0].forecast_date == date(2026, 11, 1)
    assert days[0].precip_prob == 20.0
    assert days[0].conditions == "Cloudy"


def test_estimated_forecast_is_not_a_live_update():
    profile = _FakeProfile(
        days=(_FakeDay(forecast_date=date(2026, 11, 1)),),
        quality_flag=_FakeQuality("estimated"),
    )
    days, updated, msg = _refresher_with(profile).refresh(1.0, 2.0, "X", None, None)
    assert days  # we still get the mapped days
    assert updated is False
    assert "live forecast" in msg.lower()


def test_no_days_reports_not_updated():
    profile = _FakeProfile(days=(), quality_flag=_FakeQuality("estimated"))
    days, updated, _ = _refresher_with(profile).refresh(1.0, 2.0, "X", None, None)
    assert days == [] and updated is False
