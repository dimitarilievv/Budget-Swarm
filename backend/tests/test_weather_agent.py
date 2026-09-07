"""Unit tests for WeatherAgent — forecast retrieval + normalization (BS-067).

No network: Open-Meteo is fed recorded responses via an injected transport, and
the short-TTL cache uses a FakeStore. Covers per-window WeatherProfile emission,
risk-flag derivation, caching, the weather-unchecked degrade, and the guarantee
that no itinerary decisions are made here.
"""

from __future__ import annotations

from datetime import date

from db.retrieval import CacheEntry, read_through

from ai_engine.agents.weather_agent import (
    RISK_WEATHER_CONFLICT,
    RISK_WEATHER_UNCHECKED,
    WeatherAgent,
)
from ai_engine.domain import QualityFlag, WeatherDay, WeatherProfile
from ai_engine.providers.weather import OpenMeteoProvider, WeatherService
from ai_engine.utils.api_clients import ProviderUnavailable, RetryPolicy, read_through_hook
from ai_engine.utils.geo import GeoPoint

BARCELONA = GeoPoint(lat=41.3874, lon=2.1686)

# Recorded Open-Meteo: day 0 clear, day 1 heavy rain (code 65), day 2 overcast but
# precip_prob 65 >= 60 -> both day 1 and day 2 are conflicts.
RECORDED_OM = {
    "daily": {
        "time": ["2026-09-02", "2026-09-03", "2026-09-04"],
        "temperature_2m_max": [29.5, 24.1, 26.0],
        "temperature_2m_min": [19.0, 17.5, 18.2],
        "precipitation_sum": [0.0, 12.4, 3.1],
        "precipitation_probability_max": [5, 90, 65],
        "windspeed_10m_max": [12.0, 28.0, 18.0],
        "weathercode": [0, 65, 3],
    }
}

# An all-clear window (no conflict).
CLEAR_OM = {
    "daily": {
        "time": ["2026-07-01", "2026-07-02"],
        "temperature_2m_max": [28.0, 29.0],
        "temperature_2m_min": [18.0, 19.0],
        "precipitation_sum": [0.0, 0.0],
        "precipitation_probability_max": [5, 10],
        "windspeed_10m_max": [10.0, 11.0],
        "weathercode": [0, 1],
    }
}


def _instant_retry() -> RetryPolicy:
    return RetryPolicy(sleep=lambda _d: None, jitter=lambda: 0.0)


class FakeStore:
    def __init__(self):
        self.data = {}
        self.puts = 0

    def get(self, provider, signature):
        return self.data.get((provider, signature))

    def put(self, provider, signature, body, expires_at):
        self.puts += 1
        self.data[(provider, signature)] = CacheEntry(body, None, expires_at)


def _agent(transport, *, cache=None) -> WeatherAgent:
    provider = OpenMeteoProvider(transport, retry=_instant_retry(), cache=cache)
    return WeatherAgent(WeatherService(primary=provider))


# --- per-window profile emission --------------------------------------------


def test_returns_weather_profile_for_the_window():
    agent = _agent(lambda _req: RECORDED_OM)
    profile = agent.profile_for(BARCELONA, destination="Barcelona")
    assert isinstance(profile, WeatherProfile)
    assert len(profile.days) == 3
    assert all(isinstance(d, WeatherDay) for d in profile.days)
    assert profile.quality_flag is QualityFlag.REAL


def test_day_fields_are_normalized():
    agent = _agent(lambda _req: RECORDED_OM)
    day0 = agent.profile_for(BARCELONA).days[0]
    assert day0.forecast_date == date(2026, 9, 2)
    assert day0.temp_min == 19.0 and day0.temp_max == 29.5
    assert day0.precipitation_mm == 0.0
    assert day0.precip_probability == 5  # mapped from provider precip_prob
    assert day0.wind_speed == 12.0
    assert day0.conditions == "clear"


# --- risk flags (reporting only) --------------------------------------------


def test_weather_conflict_flagged_when_any_day_is_wet():
    agent = _agent(lambda _req: RECORDED_OM)
    assert RISK_WEATHER_CONFLICT in agent.profile_for(BARCELONA).risk_flags


def test_no_conflict_flag_for_clear_window():
    agent = _agent(lambda _req: CLEAR_OM)
    assert agent.profile_for(BARCELONA).risk_flags == ()


# --- no itinerary decisions -------------------------------------------------


def test_days_preserved_in_order_no_scheduling():
    # The agent reports the forecast verbatim (same days, same order) and makes no
    # itinerary decisions — it only maps + flags.
    agent = _agent(lambda _req: RECORDED_OM)
    dates = [d.forecast_date for d in agent.profile_for(BARCELONA).days]
    assert dates == [date(2026, 9, 2), date(2026, 9, 3), date(2026, 9, 4)]
    assert dates == sorted(dates)


# --- caching ----------------------------------------------------------------


def test_cached_on_repeat_fetch():
    store = FakeStore()
    hook = read_through_hook(read_through, store)
    agent = _agent(lambda _req: RECORDED_OM, cache=hook)

    first = agent.profile_for(BARCELONA, destination="Barcelona")
    second = agent.profile_for(BARCELONA, destination="Barcelona")
    assert first.quality_flag is QualityFlag.REAL
    assert second.quality_flag is QualityFlag.CACHED
    assert len(second.days) == len(first.days)
    assert store.puts == 1


# --- degrade: weather-unchecked ---------------------------------------------


def test_weather_unchecked_when_provider_unavailable():
    def down(_req):
        raise ProviderUnavailable("Open-Meteo down")

    agent = _agent(down)
    profile = agent.profile_for(BARCELONA, destination="Nowhere")
    assert profile.days == ()
    assert profile.risk_flags == (RISK_WEATHER_UNCHECKED,)
    assert profile.quality_flag is QualityFlag.ESTIMATED


def test_empty_forecast_is_weather_unchecked():
    agent = _agent(lambda _req: {"daily": {"time": []}})
    profile = agent.profile_for(BARCELONA)
    assert profile.days == () and profile.risk_flags == (RISK_WEATHER_UNCHECKED,)


# --- robustness: bad day skipped --------------------------------------------


def test_day_with_bad_date_is_skipped():
    body = {
        "daily": {
            "time": ["not-a-date", "2026-09-03"],
            "temperature_2m_max": [20.0, 24.1],
            "temperature_2m_min": [10.0, 17.5],
            "precipitation_sum": [0.0, 1.0],
            "precipitation_probability_max": [5, 20],
            "windspeed_10m_max": [10.0, 12.0],
            "weathercode": [0, 1],
        }
    }
    agent = _agent(lambda _req: body)
    profile = agent.profile_for(BARCELONA)
    assert [d.forecast_date for d in profile.days] == [date(2026, 9, 3)]
