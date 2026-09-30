"""End-to-end integration test for the full trip engine (BS-080).

No network: the agents are injected fakes returning domain models. Proves the whole
pipeline (Scout → parallel agents → coordinator → scorer → optimizer → explanation)
returns the api.md-shaped structured plan, that the output validates against the real
``TripResponse`` schema, that ``meta`` carries degraded_sources + timing, that a failing
agent degrades rather than crashes, and that no feasible plan raises NoPlanAvailableError.
"""

from __future__ import annotations

from datetime import date, timedelta

import pytest

# The real wire contract (backend package is on the test path).
from schemas.trip_schema import TripResponse

from ai_engine.core.engine import TripPlanEngine
from ai_engine.core.pipeline import AgentSet, EngineInput
from ai_engine.core.planner import NoPlanAvailableError
from ai_engine.domain import (
    Accommodation,
    Activity,
    Destination,
    DestinationFeatures,
    IndoorOutdoor,
    QualityFlag,
    TransportMode,
    TransportOption,
    WeatherDay,
    WeatherProfile,
)


def _engine_input(**kw) -> EngineInput:
    return EngineInput(
        budget=kw.pop("budget", 2000.0),
        duration_days=kw.pop("duration_days", 2),
        travelers=kw.pop("travelers", 2),
        transport_mode=kw.pop("transport_mode", TransportMode.CAR),
        preferences=kw.pop("preferences", {"culture": 1.0}),
        origin_label=kw.pop("origin_label", "Skopje"),
        vehicle_consumption=kw.pop("vehicle_consumption", 6.5),
    )


def _dest(city, culture):
    return Destination(
        city=city,
        country="Testland",
        latitude=47.5,
        longitude=19.0,
        features=DestinationFeatures(culture_score=culture),
    )


def _scout(_inp):
    # A feasible, culture-rich candidate and a transport-heavy (over-budget) runner-up.
    return [_dest("Budapest", 0.9), _dest("Faraway", 0.95)]


def _transport(_inp, dest):
    cost = 150.0 if dest.city == "Budapest" else 5000.0  # Faraway blows the budget
    return TransportOption(
        mode=TransportMode.CAR, cost=cost, duration_minutes=600, distance_km=1300.0
    )


def _stay(_inp, dest):
    return [Accommodation(name="Central flat", price_per_night=60.0, rating=4.6)]


def _experience(_inp, dest):
    return [
        Activity(name="Buda Castle", category="castle", indoor_outdoor=IndoorOutdoor.OUTDOOR),
        Activity(name="Thermal Bath", category="spa", cost=20.0),
    ]


def _weather(_inp, dest):
    return WeatherProfile(
        days=[
            WeatherDay(
                forecast_date=date(2026, 9, 1),
                temp_min=14.0,
                temp_max=23.0,
                precip_probability=80.0,
                conditions="rain",
                weather_conflict=True,
            ),
            WeatherDay(
                forecast_date=date(2026, 9, 2),
                temp_min=15.0,
                temp_max=24.0,
                precip_probability=10.0,
                conditions="clear",
            ),
        ]
    )


def _agents(**overrides) -> AgentSet:
    return AgentSet(
        scout=overrides.get("scout", _scout),
        transport=overrides.get("transport", _transport),
        stay=overrides.get("stay", _stay),
        experience=overrides.get("experience", _experience),
        weather=overrides.get("weather", _weather),
        weather_window=overrides.get("weather_window"),
        flight_details=overrides.get("flight_details"),
    )


# --- full pipeline returns the final plan -----------------------------------


def test_full_pipeline_returns_a_structured_plan():
    result = TripPlanEngine(_agents()).generate_trip(_engine_input())
    assert set(result) == {"chosen_plan", "ranked_candidates", "explanation", "meta"}
    assert result["chosen_plan"]["destination"]["city"] == "Budapest"  # the feasible best
    assert result["chosen_plan"]["total_cost"] > 0
    assert result["explanation"] and "Budapest" in result["explanation"]


def test_output_validates_against_the_wire_schema():
    # The engine's dict must be a valid api.md TripResponse (from_attributes end to end).
    result = TripPlanEngine(_agents()).generate_trip(_engine_input())
    response = TripResponse.model_validate(result)
    assert response.chosen_plan.destination.city == "Budapest"
    assert response.chosen_plan.cost_breakdown.transport == pytest.approx(150.0)
    assert response.chosen_plan.itinerary  # scheduled activities present
    assert response.chosen_plan.weather  # forecast present


def test_accommodation_is_costed_on_nights_not_days():
    # duration_days=4 -> 3 billed nights; accommodation = nightly(60) * 3 = 180
    # (not * 4 days = 240), proving nights and duration_days are distinct (ADR-014).
    result = TripPlanEngine(_agents()).generate_trip(_engine_input(duration_days=4))
    assert result["chosen_plan"]["cost_breakdown"]["accommodation"] == pytest.approx(180.0)


def test_meta_has_degraded_sources_and_timing():
    result = TripPlanEngine(_agents()).generate_trip(_engine_input())
    meta = result["meta"]
    assert isinstance(meta["degraded_sources"], list)
    assert isinstance(meta["elapsed_ms"], int) and meta["elapsed_ms"] >= 0
    assert meta["generated_at"] is not None
    assert meta["trip_id"] is None


def test_flight_details_attached_for_air_mode():
    # For air mode the engine calls the flight-details hook (both legs + booking link) and
    # attaches its payload to the transport, and it validates against the wire schema.
    def air_transport(_inp, dest):
        return TransportOption(
            mode=TransportMode.AIRPLANE, cost=180.0, duration_minutes=210, distance_km=1300.0
        )

    def fd(_inp, dest, start, end):
        return {
            "origin": {"code": "SKP", "city": "Skopje", "airport": "Skopje Airport"},
            "destination": {"code": "IST", "city": dest.city, "airport": "Istanbul Airport"},
            "outbound": {
                "date": start,
                "depart_time": "12:45",
                "arrival_time": "14:30",
                "duration_min": 105,
                "stops": 0,
            },
            "inbound": {
                "date": end,
                "depart_time": "16:35",
                "arrival_time": "18:45",
                "duration_min": 130,
                "stops": 1,
            },
            "booking_url": "https://www.aviasales.com/search/SKP0110IST0510x1",
        }

    result = TripPlanEngine(_agents(transport=air_transport, flight_details=fd)).generate_trip(
        _engine_input(transport_mode=TransportMode.AIRPLANE)
    )
    TripResponse.model_validate(result)
    f = result["chosen_plan"]["transport"]["flight"]
    assert f["origin"]["code"] == "SKP" and f["destination"]["code"] == "IST"
    assert f["outbound"]["depart_time"] == "12:45"
    assert f["inbound"]["stops"] == 1
    assert f["booking_url"].startswith("https://www.aviasales.com/search/")


def test_flight_details_absent_for_non_air_modes():
    calls = {"n": 0}

    def fd(*_args):
        calls["n"] += 1
        return {}

    result = TripPlanEngine(_agents(flight_details=fd)).generate_trip(_engine_input())  # car
    assert result["chosen_plan"]["transport"]["flight"] is None
    assert calls["n"] == 0  # hook not called for non-air modes


def test_trip_window_uses_transport_departure_and_duration():
    # The trip's calendar dates are derived (not user input): the start comes from the
    # transport's indicative departure timestamp (e.g. the cheapest-fare date), the end is
    # duration_days on-the-ground days later (inclusive), and the departure clock time is
    # surfaced when present.
    def dated_transport(_inp, dest):
        return TransportOption(
            mode=TransportMode.CAR,
            cost=150.0,
            duration_minutes=600,
            departure_at="2026-12-01T10:20:00+00:00",
        )

    result = TripPlanEngine(_agents(transport=dated_transport)).generate_trip(
        _engine_input(duration_days=5)
    )
    cp = result["chosen_plan"]
    assert cp["start_date"] == date(2026, 12, 1)
    assert cp["end_date"] == date(2026, 12, 5)  # 5-day trip, inclusive
    assert cp["transport"]["depart_time"] == "10:20"


def test_weather_is_realigned_to_the_trip_window():
    # For a future (air) departure, the chosen plan's forecast is re-fetched for the actual
    # travel dates and used for the shown weather + scheduling, not the fan-out "today" one.
    def dated_transport(_inp, dest):
        return TransportOption(
            mode=TransportMode.CAR,
            cost=150.0,
            duration_minutes=600,
            departure_at="2026-12-01T09:00:00+00:00",
        )

    captured: dict[str, tuple[str, str]] = {}

    def window_weather(_inp, dest, start, end):
        captured["window"] = (start, end)
        return WeatherProfile(
            days=[
                WeatherDay(
                    forecast_date=date(2026, 12, 1),
                    temp_min=1.0,
                    temp_max=5.0,
                    conditions="snow",
                )
            ]
        )

    result = TripPlanEngine(
        _agents(transport=dated_transport, weather_window=window_weather)
    ).generate_trip(_engine_input(duration_days=5))

    assert captured["window"] == ("2026-12-01", "2026-12-05")  # derived from the fare date
    weather = result["chosen_plan"]["weather"]
    assert len(weather) == 1 and weather[0]["forecast_date"] == date(2026, 12, 1)


def test_weather_window_not_used_when_the_trip_starts_today():
    # When the (real) departure date is today, the fan-out forecast already covers it, so
    # the windowed re-fetch is skipped.
    from datetime import UTC, datetime

    today = datetime.now(UTC).date().isoformat()
    calls = {"n": 0}

    def today_transport(_inp, dest):
        return TransportOption(mode=TransportMode.CAR, cost=150.0, departure_at=today)

    def window_weather(_inp, dest, start, end):
        calls["n"] += 1
        return None

    TripPlanEngine(_agents(transport=today_transport, weather_window=window_weather)).generate_trip(
        _engine_input()
    )
    assert calls["n"] == 0


def test_trip_window_defaults_to_a_lead_time_without_a_departure():
    # No departure timestamp (car/bus, or an estimated flight): the window starts a few days
    # out (not today, which is unrealistic) and no departure time is shown.
    from datetime import UTC, datetime

    result = TripPlanEngine(_agents()).generate_trip(_engine_input(duration_days=3))
    cp = result["chosen_plan"]
    expected_start = datetime.now(UTC).date() + timedelta(days=3)
    assert cp["start_date"] == expected_start
    assert cp["end_date"] == expected_start + timedelta(days=2)
    assert cp["transport"]["depart_time"] is None


def test_ranked_candidates_include_the_infeasible_runner_up():
    result = TripPlanEngine(_agents()).generate_trip(_engine_input())
    ranked = {rc["destination"]["city"]: rc for rc in result["ranked_candidates"]}
    assert ranked["Budapest"]["feasible"] is True
    assert ranked["Faraway"]["feasible"] is False  # over budget, shown for transparency


def test_weather_reschedule_flows_into_the_itinerary():
    result = TripPlanEngine(_agents()).generate_trip(_engine_input())
    activities = [a for day in result["chosen_plan"]["itinerary"] for a in day["activities"]]
    castle = next(a for a in activities if a["name"] == "Buda Castle")
    # Day 1 (index 0) rains; the outdoor castle is rescheduled onto the clear day.
    assert castle["was_rescheduled"] is True
    assert ":" in castle["start_time"]  # HH:MM


# --- degrade, don't crash ---------------------------------------------------


def test_failing_agent_degrades_and_is_surfaced_in_meta():
    # A non-fatal part (weather) failing must degrade, not crash: the plan is still
    # produced and the missing part is surfaced honestly. (Lodging is a hard
    # requirement — see test_candidate_without_accommodation_is_excluded below.)
    def boom_weather(_inp, dest):
        raise RuntimeError("weather source down")

    result = TripPlanEngine(_agents(weather=boom_weather)).generate_trip(_engine_input())
    # Still a valid plan...
    TripResponse.model_validate(result)
    # ...with the missing weather surfaced honestly.
    assert any("weather" in s for s in result["meta"]["degraded_sources"])
    assert result["chosen_plan"]["weather"] == []


def test_candidate_without_accommodation_is_excluded_not_chosen():
    # A destination with no lodging must never be selected — even when it scores higher
    # — because counting its lodging as zero would make it spuriously the cheapest.
    def scout(_inp):
        # "NoBeds" is the more attractive (higher culture) but has nowhere to stay.
        return [_dest("NoBeds", 0.99), _dest("Budapest", 0.5)]

    def cheap_transport(_inp, dest):
        return TransportOption(
            mode=TransportMode.CAR, cost=150.0, duration_minutes=600, distance_km=1300.0
        )

    def stay(_inp, dest):
        return [] if dest.city == "NoBeds" else [Accommodation(name="Flat", price_per_night=60.0)]

    result = TripPlanEngine(
        _agents(scout=scout, transport=cheap_transport, stay=stay)
    ).generate_trip(_engine_input())
    assert result["chosen_plan"]["destination"]["city"] == "Budapest"
    ranked = {rc["destination"]["city"]: rc for rc in result["ranked_candidates"]}
    assert ranked["NoBeds"]["feasible"] is False  # excluded for having no accommodation
    assert ranked["Budapest"]["feasible"] is True


def test_no_accommodation_anywhere_raises_no_plan_available():
    # When no candidate has any lodging, there is no plan (rather than a trip with
    # nowhere to sleep) — the documented NO_PLAN_AVAILABLE path.
    def stay(_inp, dest):
        return []

    with pytest.raises(NoPlanAvailableError):
        TripPlanEngine(_agents(stay=stay)).generate_trip(_engine_input())


def test_estimated_quality_is_surfaced_in_degraded_sources():
    def estimated_transport(_inp, dest):
        return TransportOption(
            mode=TransportMode.CAR, cost=150.0, duration_minutes=600, quality_flag="estimated"
        )

    result = TripPlanEngine(_agents(transport=estimated_transport)).generate_trip(_engine_input())
    assert any("transport:estimated" == s for s in result["meta"]["degraded_sources"])


# --- fallback chain integration: force each provider to fail (BS-092) --------
#
# Two failure shapes per §22: a provider whose primary fails but whose fallback
# supplies an ESTIMATE (transport, accommodation — the chain degrades, the piece is
# present and flagged), and a source that is fully DOWN (stay/experience/weather —
# the piece degrades to missing). Either way a valid, schema-conformant plan is still
# produced and the degradation is surfaced in meta.degraded_sources.


def _plan(agents: AgentSet) -> dict:
    """Generate a plan and assert it always conforms to the wire schema."""
    result = TripPlanEngine(agents).generate_trip(_engine_input())
    TripResponse.model_validate(result)  # never returns a malformed plan
    return result


def _estimated_transport(_inp, dest):
    cost = 150.0 if dest.city == "Budapest" else 5000.0  # keep Budapest feasible
    return TransportOption(
        mode=TransportMode.CAR,
        cost=cost,
        duration_minutes=600,
        distance_km=1300.0,
        quality_flag=QualityFlag.ESTIMATED,
    )


def _estimated_stay(_inp, dest):
    return [
        Accommodation(
            name="Estimated flat",
            price_per_night=60.0,
            rating=4.0,
            quality_flag=QualityFlag.ESTIMATED,
        )
    ]


@pytest.mark.parametrize(
    "overrides, expected",
    [
        ({"transport": _estimated_transport}, "transport:estimated"),
        ({"stay": _estimated_stay}, "accommodation:estimated"),
    ],
)
def test_estimated_fallback_still_plans_and_is_flagged(overrides, expected):
    result = _plan(_agents(**overrides))
    assert expected in result["meta"]["degraded_sources"]
    assert result["chosen_plan"]["total_cost"] <= _engine_input().budget


def _raiser(message: str):
    def _fn(_inp, dest):
        raise RuntimeError(message)

    return _fn


# Only the *soft* parts are covered here: activities and weather degrade to a thin plan.
# Accommodation is a hard requirement (a trip needs somewhere to stay), so a downed stay
# source for every candidate yields no plan instead — see
# test_no_accommodation_anywhere_raises_no_plan_available.
@pytest.mark.parametrize(
    "agent, part",
    [
        ("experience", "activities"),
        ("weather", "weather"),
    ],
)
def test_source_down_degrades_that_part_and_still_plans(agent, part):
    result = _plan(_agents(**{agent: _raiser(f"{part} source down")}))
    # A plan is still produced, with the failed part surfaced honestly.
    assert result["chosen_plan"] is not None
    assert any(part in s for s in result["meta"]["degraded_sources"])


def test_multiple_simultaneous_failures_still_yield_a_valid_flagged_plan():
    # Transport degrades to an estimate, lodging falls back to a price-stats estimate, and
    # the weather source is down — all at once. The pipeline must still return one valid
    # plan, flagging every degraded part. (Lodging must still resolve to *something*, since
    # a stay is a hard requirement; a fully-absent stay is the no-plan case tested above.)
    result = _plan(
        _agents(
            transport=_estimated_transport,
            stay=_estimated_stay,
            weather=_raiser("forecast down"),
        )
    )
    degraded = result["meta"]["degraded_sources"]
    assert "transport:estimated" in degraded
    assert any("accommodation" in s for s in degraded)
    assert any("weather" in s for s in degraded)


# --- no feasible plan -------------------------------------------------------


def test_no_candidates_raises_no_plan_available():
    with pytest.raises(NoPlanAvailableError):
        TripPlanEngine(_agents(scout=lambda _inp: [])).generate_trip(_engine_input())


def test_all_over_budget_raises_no_plan_available():
    def pricey(_inp, dest):
        return TransportOption(mode=TransportMode.CAR, cost=9000.0, duration_minutes=600)

    with pytest.raises(NoPlanAvailableError):
        TripPlanEngine(_agents(transport=pricey)).generate_trip(_engine_input(budget=500.0))
