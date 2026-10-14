"""Engine-level tests for the post-selection stay_window hook (BS-calendar).

Drive the real TripPlanEngine with a mock stay_window (the DB/calendar boundary is
mocked) to verify: the hook fires with the derived travel window, snapshot availability
+ calendar price reach the response, backward compatibility when the hook is absent or
returns None, no call when the trip starts today, failure falls back to a valid plan,
and the hard budget is never violated after an accommodation update.
"""

from __future__ import annotations

from datetime import UTC, datetime

from ai_engine.core.engine import TripPlanEngine
from ai_engine.core.pipeline import AgentSet, EngineInput
from ai_engine.domain import (
    Accommodation,
    Activity,
    Destination,
    DestinationFeatures,
    IndoorOutdoor,
    TransportMode,
    TransportOption,
)


def _engine_input(**kw) -> EngineInput:
    return EngineInput(
        budget=kw.pop("budget", 2000.0),
        duration_days=kw.pop("duration_days", 2),
        travelers=kw.pop("travelers", 2),
        transport_mode=TransportMode.CAR,
        preferences={"culture": 1.0},
        origin_label="Skopje",
        vehicle_consumption=6.5,
    )


def _dest(city):
    return Destination(
        city=city,
        country="Testland",
        latitude=47.5,
        longitude=19.0,
        features=DestinationFeatures(culture_score=0.9),
    )


def _scout(_inp):
    return [_dest("Budapest")]


def _transport(_inp, dest, departure_at="2026-12-01"):
    return TransportOption(
        mode=TransportMode.CAR,
        cost=150.0,
        duration_minutes=600,
        distance_km=1300.0,
        departure_at=departure_at,
    )


def _stay(_inp, dest):
    return [Accommodation(name="Central flat", price_per_night=60.0, rating=4.6)]


def _experience(_inp, dest):
    return [Activity(name="Castle", category="castle", indoor_outdoor=IndoorOutdoor.OUTDOOR)]


def _weather(_inp, dest):
    return None


def _agents(**overrides) -> AgentSet:
    return AgentSet(
        scout=_scout,
        transport=overrides.get("transport", _transport),
        stay=_stay,
        experience=_experience,
        weather=_weather,
        stay_window=overrides.get("stay_window"),
    )


def test_hook_fires_and_snapshot_availability_reaches_response():
    calls = []

    def sw(_inp, dest, start, end, current):
        calls.append((dest.city, start, end))
        return [
            Accommodation(
                name="Snap flat",
                price_per_night=80.0,
                rating=4.5,
                availability_status="snapshot_available",
                snapshot_date="2026-09-22",
            )
        ]

    acc = TripPlanEngine(_agents(stay_window=sw)).generate_trip(_engine_input())["chosen_plan"][
        "accommodation"
    ]
    assert calls and calls[0][0] == "Budapest"
    assert acc["availability"] == "snapshot_available"
    assert acc["snapshot_date"] == "2026-09-22"
    assert acc["price_per_night"] == 80.0


def test_absent_hook_is_backward_compatible():
    acc = TripPlanEngine(_agents()).generate_trip(_engine_input())["chosen_plan"]["accommodation"]
    assert acc["availability"] == "availability_unknown" and acc["name"] == "Central flat"


def test_hook_not_called_when_trip_starts_today():
    calls = []

    def sw(*args):
        calls.append(args)
        return None

    def today_transport(_inp, dest):
        return _transport(_inp, dest, departure_at=datetime.now(UTC).date().isoformat())

    TripPlanEngine(_agents(transport=today_transport, stay_window=sw)).generate_trip(
        _engine_input()
    )
    assert calls == []


def test_hook_returning_none_keeps_current():
    acc = TripPlanEngine(_agents(stay_window=lambda *_: None)).generate_trip(_engine_input())[
        "chosen_plan"
    ]["accommodation"]
    assert acc["name"] == "Central flat" and acc["availability"] == "availability_unknown"


def test_hook_failure_falls_back_to_valid_plan():
    def sw(*_args):
        raise RuntimeError("calendar boom")

    acc = TripPlanEngine(_agents(stay_window=sw)).generate_trip(_engine_input())["chosen_plan"][
        "accommodation"
    ]
    assert acc["name"] == "Central flat"


def test_over_budget_update_falls_back_within_budget():
    def sw(_inp, dest, start, end, current):
        return [Accommodation(name="Penthouse", price_per_night=100000.0)]

    cp = TripPlanEngine(_agents(stay_window=sw)).generate_trip(_engine_input(budget=500.0))[
        "chosen_plan"
    ]
    assert cp["accommodation"]["name"] == "Central flat"
    assert cp["total_cost"] <= 500.0
