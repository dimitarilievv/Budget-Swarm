"""Unit tests for the CP-SAT activity scheduler (BS-073).

Deterministic, no I/O. Covers per-day time-budget enforcement, no-overlap
placement, opening-hours respect (and its absence), the value-weighted selection
when time is scarce, unschedulable activities, and the opening-hours parser. The
constraint-violation cases are verified by asserting the *output* never violates
a constraint — an over-budget or overlapping schedule is never returned.
"""

from __future__ import annotations

import itertools

import pytest

from ai_engine.core.optimizer import SolveStatus
from ai_engine.core.scheduler import (
    ActivityScheduler,
    SchedulingPolicy,
    parse_opening_hours,
)
from ai_engine.domain import Activity


def _activity(
    name: str, *, minutes: int | None = 120, opening_hours: str | None = None
) -> Activity:
    return Activity(name=name, est_duration_minutes=minutes, opening_hours=opening_hours)


def _no_overlaps(items) -> bool:
    """True if no two placed activities on the same day overlap in time."""
    for a, b in itertools.combinations(items, 2):
        if a.day == b.day and a.start_minute < b.end_minute and b.start_minute < a.end_minute:
            return False
    return True


# --- basic placement --------------------------------------------------------


def test_activities_that_fit_are_all_scheduled():
    acts = [_activity("A"), _activity("B"), _activity("C")]  # 3 * 120 = 360 <= 720
    result = ActivityScheduler().schedule(acts, days=1)
    assert result.status is SolveStatus.OPTIMAL
    assert result.scheduled_count == 3
    assert result.unscheduled == ()
    assert _no_overlaps(result.items)


def test_placed_items_lie_within_the_day_window():
    result = ActivityScheduler().schedule([_activity("A")], days=1)
    item = result.items[0]
    assert item.start_minute >= 9 * 60
    assert item.end_minute <= 21 * 60
    assert item.end_minute - item.start_minute == 120


# --- per-day time budget ----------------------------------------------------


def test_per_day_time_budget_limits_activities():
    # 200-minute budget, 120-minute activities -> only one fits per day.
    policy = SchedulingPolicy(available_minutes=200)
    acts = [_activity("A"), _activity("B"), _activity("C")]
    result = ActivityScheduler(policy).schedule(acts, days=1)
    assert result.scheduled_count == 1
    # The placed day's total duration never exceeds the budget.
    per_day = result.by_day()
    assert sum(it.end_minute - it.start_minute for it in per_day[0]) <= 200


def test_budget_spreads_activities_across_days():
    policy = SchedulingPolicy(available_minutes=200)  # 1 activity/day
    acts = [_activity(n) for n in ("A", "B", "C", "D")]
    result = ActivityScheduler(policy).schedule(acts, days=4)
    assert result.scheduled_count == 4
    # At most one per day given the budget.
    assert all(len(day) <= 1 for day in result.by_day())
    assert _no_overlaps(result.items)


# --- no overlaps ------------------------------------------------------------


def test_no_overlaps_when_many_share_a_day():
    # 5 * 120 = 600 <= 720, all fit one day; must be laid out without overlap.
    acts = [_activity(n) for n in ("A", "B", "C", "D", "E")]
    result = ActivityScheduler().schedule(acts, days=1)
    assert result.scheduled_count == 5
    assert _no_overlaps(result.items)


# --- opening hours ----------------------------------------------------------


def test_opening_hours_constrain_placement():
    # Open 09:00–11:00 only; a 120-minute visit must be exactly 540–660.
    act = _activity("Museum", minutes=120, opening_hours="Mo-Su 09:00-11:00")
    result = ActivityScheduler().schedule([act], days=1)
    item = result.items[0]
    assert item.start_minute == 9 * 60
    assert item.end_minute == 11 * 60


def test_opening_window_too_small_leaves_activity_unscheduled():
    # Open only 09:00–10:00 (60 min) but the visit needs 120 -> cannot be placed.
    act = _activity("Tiny", minutes=120, opening_hours="09:00-10:00")
    result = ActivityScheduler().schedule([act], days=1)
    assert result.scheduled_count == 0
    assert act in result.unscheduled


def test_absent_opening_hours_are_unconstrained():
    act = _activity("Anytime", minutes=120, opening_hours=None)
    result = ActivityScheduler().schedule([act], days=1)
    assert result.scheduled_count == 1
    # Free to sit anywhere in the 09:00–21:00 window.
    assert 9 * 60 <= result.items[0].start_minute <= 21 * 60 - 120


def test_opening_hours_respected_alongside_free_activities():
    constrained = _activity("Early", minutes=120, opening_hours="09:00-13:00")
    free = _activity("Free", minutes=120)
    result = ActivityScheduler().schedule([constrained, free], days=1)
    placed = {it.activity.name: it for it in result.items}
    assert placed["Early"].start_minute >= 9 * 60
    assert placed["Early"].end_minute <= 13 * 60
    assert _no_overlaps(result.items)


# --- weighted selection -----------------------------------------------------


def test_scarce_time_keeps_the_most_valuable_activity():
    policy = SchedulingPolicy(available_minutes=120)  # room for exactly one
    low = _activity("Low")
    high = _activity("High")
    result = ActivityScheduler(policy).schedule([low, high], days=1, weights=[0.2, 0.9])
    assert result.scheduled_count == 1
    assert result.items[0].activity.name == "High"


def test_equal_weight_fills_available_time():
    # No weights supplied: the scheduler still maximizes activities done.
    policy = SchedulingPolicy(available_minutes=360)  # 3 * 120
    acts = [_activity(n) for n in ("A", "B", "C", "D")]
    result = ActivityScheduler(policy).schedule(acts, days=1)
    assert result.scheduled_count == 3


# --- duration handling ------------------------------------------------------


def test_missing_duration_uses_policy_default():
    policy = SchedulingPolicy(default_activity_minutes=90)
    act = _activity("Unknown", minutes=None)
    result = ActivityScheduler(policy).schedule([act], days=1)
    assert result.items[0].end_minute - result.items[0].start_minute == 90


def test_activity_longer_than_budget_is_unscheduled():
    policy = SchedulingPolicy(available_minutes=120)
    act = _activity("Marathon", minutes=300)
    result = ActivityScheduler(policy).schedule([act], days=1)
    assert result.scheduled_count == 0
    assert act in result.unscheduled


# --- edges / validation -----------------------------------------------------


def test_empty_activities_returns_empty_schedule():
    result = ActivityScheduler().schedule([], days=3)
    assert result.status is SolveStatus.OPTIMAL
    assert result.items == ()
    assert result.scheduled_count == 0


def test_days_below_one_rejected():
    with pytest.raises(ValueError, match="days"):
        ActivityScheduler().schedule([_activity("A")], days=0)


def test_weights_length_must_match():
    with pytest.raises(ValueError, match="weights length"):
        ActivityScheduler().schedule([_activity("A"), _activity("B")], days=1, weights=[0.5])


def test_invalid_policy_window_rejected():
    with pytest.raises(ValueError, match="day window"):
        SchedulingPolicy(day_start_minute=1200, day_end_minute=600)


def test_available_minutes_beyond_window_rejected():
    with pytest.raises(ValueError, match="available_minutes"):
        SchedulingPolicy(day_start_minute=540, day_end_minute=660, available_minutes=999)


def test_schedule_is_deterministic():
    acts = [_activity(n) for n in ("A", "B", "C")]
    policy = SchedulingPolicy(available_minutes=240)
    r1 = ActivityScheduler(policy).schedule(acts, days=1, weights=[0.5, 0.5, 0.9])
    r2 = ActivityScheduler(policy).schedule(acts, days=1, weights=[0.5, 0.5, 0.9])
    assert [it.activity.name for it in r1.items] == [it.activity.name for it in r2.items]


# --- opening-hours parser ---------------------------------------------------


@pytest.mark.parametrize(
    "text,expected",
    [
        (None, None),
        ("", None),
        ("closed", None),
        ("24/7", (0, 1440)),
        ("09:00-17:00", (540, 1020)),
        ("9:00-17:00", (540, 1020)),
        ("Mo-Su 09:00-18:00", (540, 1080)),
        ("Mo-Fr 08:00-12:00,13:00-17:00", (480, 1020)),  # span across the lunch gap
        ("20:00-02:00", None),  # overnight / close <= open -> ignored
    ],
)
def test_parse_opening_hours(text, expected):
    assert parse_opening_hours(text) == expected
