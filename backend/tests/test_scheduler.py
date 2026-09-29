"""Unit tests for the CP-SAT activity scheduler (BS-073).

Deterministic, no I/O. Covers per-day time-budget enforcement, no-overlap
placement, opening-hours respect (and its absence), the value-weighted selection
when time is scarce, unschedulable activities, and the opening-hours parser. The
constraint-violation cases are verified by asserting the *output* never violates
a constraint — an over-budget or overlapping schedule is never returned.
"""

from __future__ import annotations

import itertools
from datetime import date

import pytest

from ai_engine.core.optimizer import SolveStatus
from ai_engine.core.scheduler import (
    ActivityScheduler,
    SchedulingPolicy,
    parse_opening_hours,
    weather_conflict_flags,
)
from ai_engine.domain import Activity, IndoorOutdoor, WeatherDay, WeatherProfile


def _activity(
    name: str,
    *,
    minutes: int | None = 120,
    opening_hours: str | None = None,
    indoor_outdoor: IndoorOutdoor = IndoorOutdoor.MIXED,
) -> Activity:
    return Activity(
        name=name,
        est_duration_minutes=minutes,
        opening_hours=opening_hours,
        indoor_outdoor=indoor_outdoor,
    )


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


def test_activities_spread_across_days_by_default():
    # 6 * 120 = 720 would all fit a single day's time budget, but the default per-day
    # cap (ceil(6/3) = 2) spreads them evenly instead of clustering onto one day.
    acts = [_activity(n) for n in ("A", "B", "C", "D", "E", "F")]
    result = ActivityScheduler().schedule(acts, days=3)
    assert result.scheduled_count == 6
    assert all(len(day) == 2 for day in result.by_day())  # 2 per day, not 6 on one


def test_explicit_max_activities_per_day_is_respected():
    policy = SchedulingPolicy(max_activities_per_day=1)
    acts = [_activity(n) for n in ("A", "B", "C")]
    result = ActivityScheduler(policy).schedule(acts, days=3)
    assert all(len(day) <= 1 for day in result.by_day())


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
        ("24/7", ((0, 1440),)),
        ("09:00-17:00", ((540, 1020),)),
        ("9:00-17:00", ((540, 1020),)),
        ("Mo-Su 09:00-18:00", ((540, 1080),)),
        # A lunch-break split yields both windows (not the encompassing span).
        ("Mo-Fr 08:00-12:00,13:00-17:00", ((480, 720), (780, 1020))),
        ("20:00-02:00", None),  # overnight / close <= open -> ignored
    ],
)
def test_parse_opening_hours(text, expected):
    assert parse_opening_hours(text) == expected


# --- weather constraint + dynamic replanning (BS-074) -----------------------

_OUTDOOR = IndoorOutdoor.OUTDOOR
_INDOOR = IndoorOutdoor.INDOOR
_MIXED = IndoorOutdoor.MIXED


def test_no_outdoor_activity_on_a_conflict_day():
    # Day 1 is a weather conflict; no outdoor activity may land on it.
    policy = SchedulingPolicy(available_minutes=240)  # 2 slots/day
    acts = [
        _activity("O1", indoor_outdoor=_OUTDOOR),
        _activity("O2", indoor_outdoor=_OUTDOOR),
        _activity("I1", indoor_outdoor=_INDOOR),
    ]
    result = ActivityScheduler(policy).schedule(acts, days=2, weather_conflicts=[False, True])
    for item in result.items:
        if item.activity.indoor_outdoor is _OUTDOOR:
            assert item.day != 1


def test_single_outdoor_moves_to_the_clear_day():
    # Day 0 conflict, day 1 clear -> the only outdoor activity must sit on day 1.
    result = ActivityScheduler().schedule(
        [_activity("Sole", indoor_outdoor=_OUTDOOR)],
        days=2,
        weather_conflicts=[True, False],
    )
    assert result.scheduled_count == 1
    assert result.items[0].day == 1


def test_outdoor_dropped_and_indoor_swapped_in_is_flagged():
    # One slot, one conflict day: the higher-value outdoor wins the slot weather-naively,
    # but the forecast forbids it -> the indoor alternative is swapped in and flagged.
    policy = SchedulingPolicy(available_minutes=120)  # one slot on the single day
    outdoor = _activity("Castle", indoor_outdoor=_OUTDOOR)
    indoor = _activity("Museum", indoor_outdoor=_INDOOR)
    result = ActivityScheduler(policy).schedule(
        [outdoor, indoor], days=1, weights=[0.9, 0.5], weather_conflicts=[True]
    )

    assert result.status is SolveStatus.OPTIMAL
    # The outdoor activity is not placed on the conflict day (it is dropped).
    assert all(it.activity.name != "Castle" for it in result.items)
    assert outdoor in result.unscheduled
    # The indoor replacement is scheduled and recorded as rescheduled (a weather swap-in).
    museum = next(it for it in result.items if it.activity.name == "Museum")
    assert museum.day == 0
    assert museum.was_rescheduled is True


def test_indoor_activity_allowed_on_conflict_day_not_flagged():
    result = ActivityScheduler().schedule(
        [_activity("Gallery", indoor_outdoor=_INDOOR)], days=1, weather_conflicts=[True]
    )
    assert result.scheduled_count == 1
    assert result.items[0].day == 0
    assert result.items[0].was_rescheduled is False


def test_mixed_activity_allowed_on_conflict_day():
    result = ActivityScheduler().schedule(
        [_activity("Market", indoor_outdoor=_MIXED)], days=1, weather_conflicts=[True]
    )
    assert result.scheduled_count == 1
    assert result.items[0].day == 0


def test_no_conflict_flags_means_no_reschedule():
    acts = [_activity("A", indoor_outdoor=_OUTDOOR), _activity("B", indoor_outdoor=_INDOOR)]
    result = ActivityScheduler().schedule(acts, days=2, weather_conflicts=[False, False])
    assert result.scheduled_count == 2
    assert all(not it.was_rescheduled for it in result.items)


def test_outdoor_with_every_day_conflicting_is_unscheduled_not_fatal():
    result = ActivityScheduler().schedule(
        [_activity("OnlyOutdoor", indoor_outdoor=_OUTDOOR)],
        days=2,
        weather_conflicts=[True, True],
    )
    assert result.status is SolveStatus.OPTIMAL
    assert result.scheduled_count == 0
    assert result.unscheduled  # returned, not raised


def test_weather_conflicts_length_must_match_days():
    with pytest.raises(ValueError, match="weather_conflicts length"):
        ActivityScheduler().schedule([_activity("A")], days=2, weather_conflicts=[True])


def test_weather_replan_is_deterministic():
    policy = SchedulingPolicy(available_minutes=120)
    acts = [_activity("O", indoor_outdoor=_OUTDOOR), _activity("I", indoor_outdoor=_INDOOR)]
    kwargs = dict(days=1, weights=[0.9, 0.5], weather_conflicts=[True])
    r1 = ActivityScheduler(policy).schedule(acts, **kwargs)
    r2 = ActivityScheduler(policy).schedule(acts, **kwargs)
    assert [(it.activity.name, it.day, it.was_rescheduled) for it in r1.items] == [
        (it.activity.name, it.day, it.was_rescheduled) for it in r2.items
    ]


# --- weather_conflict_flags helper ------------------------------------------


def test_weather_conflict_flags_from_profile():
    profile = WeatherProfile(
        days=[
            WeatherDay(forecast_date=date(2026, 9, 1), weather_conflict=True),
            WeatherDay(forecast_date=date(2026, 9, 2), weather_conflict=False),
            WeatherDay(forecast_date=date(2026, 9, 3), weather_conflict=True),
        ]
    )
    assert weather_conflict_flags(profile) == [True, False, True]


def test_weather_conflict_flags_empty_profile():
    assert weather_conflict_flags(WeatherProfile()) == []


# --- multi-window opening hours (lunch-break split) -------------------------


def test_activity_placed_inside_one_window_not_the_closed_gap():
    # Open 09:00–11:00 and 14:00–16:00; a 120-min visit must fill one window exactly,
    # never the closed 11:00–14:00 gap.
    act = _activity("Split", minutes=120, opening_hours="09:00-11:00,14:00-16:00")
    result = ActivityScheduler().schedule([act], days=1)
    item = result.items[0]
    assert (item.start_minute, item.end_minute) in {(540, 660), (840, 960)}


def test_activity_that_fits_no_window_is_unscheduled():
    # Two 90-min windows, but the visit needs 120 -> fits neither.
    act = _activity("TooLong", minutes=120, opening_hours="09:00-10:30,14:00-15:30")
    result = ActivityScheduler().schedule([act], days=1)
    assert result.scheduled_count == 0
    assert act in result.unscheduled


# --- transition buffer (travel between activities) --------------------------


def test_transition_buffer_charges_travel_between_activities():
    # 360-min budget, 120-min activities. With no buffer 3 fit; a 60-min buffer means
    # 120*3 + 60*2 = 480 > 360, so only 2 fit (120*2 + 60 = 300 <= 360).
    acts = [_activity(n) for n in ("A", "B", "C")]
    no_buffer = ActivityScheduler(SchedulingPolicy(available_minutes=360)).schedule(acts, days=1)
    with_buffer = ActivityScheduler(
        SchedulingPolicy(available_minutes=360, transition_buffer_minutes=60)
    ).schedule(acts, days=1)
    assert no_buffer.scheduled_count == 3
    assert with_buffer.scheduled_count == 2


def test_transition_buffer_does_not_penalize_a_single_activity():
    # A lone activity pays no transition buffer (nothing to travel between).
    policy = SchedulingPolicy(available_minutes=120, transition_buffer_minutes=60)
    result = ActivityScheduler(policy).schedule([_activity("Solo", minutes=120)], days=1)
    assert result.scheduled_count == 1


def test_negative_transition_buffer_rejected():
    with pytest.raises(ValueError, match="transition_buffer_minutes"):
        SchedulingPolicy(transition_buffer_minutes=-1)
