"""Integration tests for the Scout → parallel agent fan-out (BS-079).

No network, no pytest-asyncio: the injected agents are fakes and the coroutine is
driven with ``asyncio.run``. Covers Scout-first ordering, genuine concurrency of the
four specialists, per-agent error isolation (a failure degrades one piece only), that
one candidate's failure does not affect others, and that the bundles feed the
coordinator with the missing pieces marked degraded.
"""

from __future__ import annotations

import asyncio
import threading
import time
from datetime import date

from ai_engine.agents.coordinator_agent import AgentOutputs, CoordinatorAgent
from ai_engine.core.pipeline import AgentSet, EngineInput, gather_agent_outputs
from ai_engine.domain import (
    Accommodation,
    Activity,
    Destination,
    TransportMode,
    TransportOption,
    WeatherDay,
    WeatherProfile,
)


def _engine_input(**kw) -> EngineInput:
    return EngineInput(
        budget=kw.pop("budget", 1000.0),
        duration_days=kw.pop("duration_days", 3),
        travelers=kw.pop("travelers", 2),
        transport_mode=kw.pop("transport_mode", TransportMode.CAR),
        preferences=kw.pop("preferences", {"culture": 1.0}),
        origin_label=kw.pop("origin_label", "Skopje"),
        **kw,
    )


def _destinations(*cities: str) -> list[Destination]:
    return [Destination(city=c, country="Testland", latitude=40.0, longitude=20.0) for c in cities]


# --- default fakes ----------------------------------------------------------


def _transport(_inp, dest):
    return TransportOption(mode=TransportMode.CAR, cost=150.0, duration_minutes=600)


def _stay(_inp, dest):
    return [Accommodation(price_per_night=60.0, rating=4.5)]


def _experience(_inp, dest):
    return [Activity(name=f"{dest.city} sight")]


def _weather(_inp, dest):
    return WeatherProfile(days=[WeatherDay(forecast_date=date(2026, 9, 1))])


def _agents(**overrides) -> AgentSet:
    return AgentSet(
        scout=overrides.get("scout", lambda _inp: _destinations("A", "B", "C")),
        transport=overrides.get("transport", _transport),
        stay=overrides.get("stay", _stay),
        experience=overrides.get("experience", _experience),
        weather=overrides.get("weather", _weather),
    )


def _run(agents, *, nights=2, concurrency=8):
    return asyncio.run(
        gather_agent_outputs(_engine_input(), agents, nights=nights, concurrency=concurrency)
    )


# --- happy path -------------------------------------------------------------


def test_all_pieces_present_for_every_candidate():
    outputs = _run(_agents())
    assert [o.destination.city for o in outputs] == ["A", "B", "C"]  # Scout order preserved
    for o in outputs:
        assert isinstance(o, AgentOutputs)
        assert o.transport is not None
        assert o.accommodations and o.activities
        assert o.weather is not None
        assert o.nights == 2


def test_no_candidates_returns_empty():
    assert _run(_agents(scout=lambda _inp: [])) == []


# --- scout runs first -------------------------------------------------------


def test_scout_runs_first_then_the_specialists():
    order: list[str] = []
    lock = threading.Lock()

    def scout(_inp):
        with lock:
            order.append("scout")
        return _destinations("A")

    def spec(label):
        def fn(_inp, dest):
            with lock:
                order.append(label)
            return _transport(_inp, dest) if label == "transport" else []

        return fn

    agents = AgentSet(
        scout=scout,
        transport=spec("transport"),
        stay=spec("stay"),
        experience=spec("experience"),
        weather=spec("weather"),
    )
    asyncio.run(gather_agent_outputs(_engine_input(), agents, nights=2))
    assert order[0] == "scout"
    assert set(order[1:]) == {"transport", "stay", "experience", "weather"}


# --- genuine concurrency ----------------------------------------------------


def test_the_four_agents_run_concurrently():
    live = 0
    peak = 0
    lock = threading.Lock()

    def slow(_inp, dest):
        nonlocal live, peak
        with lock:
            live += 1
            peak = max(peak, live)
        time.sleep(0.05)
        with lock:
            live -= 1
        return None

    agents = _agents(transport=slow, stay=slow, experience=slow, weather=slow)
    asyncio.run(gather_agent_outputs(_engine_input(), agents, nights=2, concurrency=8))
    assert peak >= 2  # more than one agent call was in flight at once


def test_concurrency_limit_is_respected():
    live = 0
    peak = 0
    lock = threading.Lock()

    def slow(_inp, dest):
        nonlocal live, peak
        with lock:
            live += 1
            peak = max(peak, live)
        time.sleep(0.03)
        with lock:
            live -= 1
        return None

    agents = AgentSet(
        scout=lambda _inp: _destinations("A", "B", "C", "D", "E"),
        transport=slow,
        stay=slow,
        experience=slow,
        weather=slow,
    )
    asyncio.run(gather_agent_outputs(_engine_input(), agents, nights=2, concurrency=3))
    assert peak <= 3  # never exceeds the cap


# --- error isolation --------------------------------------------------------


def test_one_agent_failure_degrades_only_that_piece():
    def boom_transport(_inp, dest):
        raise RuntimeError("flight provider down")

    outputs = _run(_agents(transport=boom_transport))
    for o in outputs:
        assert o.transport is None  # the failed piece degraded to missing
        # ...but the other three agents still produced their data.
        assert o.accommodations and o.activities and o.weather is not None


def test_failure_is_isolated_to_the_affected_candidate():
    def transport_fails_for_b(_inp, dest):
        if dest.city == "B":
            raise RuntimeError("no route to B")
        return _transport(_inp, dest)

    outputs = {o.destination.city: o for o in _run(_agents(transport=transport_fails_for_b))}
    assert outputs["B"].transport is None
    assert outputs["A"].transport is not None
    assert outputs["C"].transport is not None


def test_all_specialists_failing_still_yields_a_bundle_per_candidate():
    def boom(_inp, dest):
        raise RuntimeError("down")

    outputs = _run(_agents(transport=boom, stay=boom, experience=boom, weather=boom))
    assert len(outputs) == 3
    for o in outputs:
        assert o.transport is None and not o.accommodations and not o.activities
        assert o.weather is None


# --- feeds the coordinator (degraded marking) -------------------------------


def test_bundles_feed_the_coordinator_with_degraded_marks():
    def boom_transport(_inp, dest):
        raise RuntimeError("down")

    outputs = _run(_agents(transport=boom_transport))
    aggregates = CoordinatorAgent().aggregate_all(outputs)
    assert len(aggregates) == 3
    for agg in aggregates:
        # A missing transport piece is marked degraded by the coordinator (BS-071).
        assert "transport" in agg.degraded
        assert not agg.is_complete
