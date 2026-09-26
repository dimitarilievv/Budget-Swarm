"""Weight-sensitivity comparison (BS Phase 11; technical-doc §25).

Shows that the user's preference weights actually steer the choice: with three candidates
that are **identical in cost, lodging, activities and weather** and differ only in their
feature profile (one strong in culture, one in food, one in nature), the optimizer's chosen
destination tracks whichever preference is emphasized. This isolates the preference weight as
the deciding factor — the "vary w1..w6, show how the choice shifts" analysis.
"""

from __future__ import annotations

import pytest

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

# Each candidate dominates exactly one dimension; everything else is held equal.
PROFILES = {
    "CultureTown": dict(culture=0.95, food=0.30, nature=0.30),
    "FoodTown": dict(culture=0.30, food=0.95, nature=0.30),
    "NatureTown": dict(culture=0.30, food=0.30, nature=0.95),
}


def _scout(_inp):
    return [
        Destination(
            city=city,
            country="Testland",
            latitude=45.0,
            longitude=20.0,
            features=DestinationFeatures(
                culture_score=p["culture"], food_score=p["food"], nature_score=p["nature"]
            ),
        )
        for city, p in PROFILES.items()
    ]


# Identical non-preference inputs for every candidate, so only preference weight differs.
def _transport(_inp, dest):
    return TransportOption(
        mode=TransportMode.CAR, cost=150.0, duration_minutes=480, distance_km=900.0
    )


def _stay(_inp, dest):
    return [Accommodation(name="Std", price_per_night=60.0, rating=4.4)]


def _experience(_inp, dest):
    return [
        Activity(name=f"{dest.city} A", indoor_outdoor=IndoorOutdoor.INDOOR),
        Activity(name=f"{dest.city} B", indoor_outdoor=IndoorOutdoor.OUTDOOR),
    ]


def _agents() -> AgentSet:
    return AgentSet(
        scout=_scout,
        transport=_transport,
        stay=_stay,
        experience=_experience,
        weather=lambda _i, _d: None,
    )


def _chosen_city(preferences: dict[str, float]) -> str:
    engine_input = EngineInput(
        budget=3000.0,
        duration_days=3,
        travelers=1,
        transport_mode=TransportMode.CAR,
        preferences=preferences,
        origin_label="Skopje",
        vehicle_consumption=6.5,
    )
    result = TripPlanEngine(_agents()).generate_trip(engine_input)
    # All three are within the generous budget → the choice is preference-driven.
    assert result["chosen_plan"]["total_cost"] <= 3000.0
    return result["chosen_plan"]["destination"]["city"]


@pytest.mark.parametrize(
    "preferences, expected",
    [
        ({"culture": 1.0}, "CultureTown"),
        ({"food": 1.0}, "FoodTown"),
        ({"nature": 1.0}, "NatureTown"),
    ],
)
def test_emphasized_preference_selects_the_matching_destination(preferences, expected):
    assert _chosen_city(preferences) == expected


def test_shifting_the_weight_shifts_the_choice():
    # The same candidate set yields three different winners as the emphasis moves.
    winners = {
        _chosen_city({"culture": 1.0}),
        _chosen_city({"food": 1.0}),
        _chosen_city({"nature": 1.0}),
    }
    assert winners == {"CultureTown", "FoodTown", "NatureTown"}


def test_blended_preferences_favor_the_blend_leader():
    # Culture-heavy but with some food: the culture-dominant town still wins.
    assert _chosen_city({"culture": 0.8, "food": 0.2}) == "CultureTown"
    # Food-heavy flips it to the food town.
    assert _chosen_city({"culture": 0.2, "food": 0.8}) == "FoodTown"
