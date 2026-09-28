"""OR-Tools / CP-SAT activity scheduling — per-day selection + time-slot placement (BS-073).

Once the optimizer has chosen a destination (BS-072), this model decides **which
activities to do and when** within the stay (technical-doc §15.1/§15.4): a boolean
``take[a]`` per activity, each taken activity assigned to a day and a time slot,
subject to the scheduling hard constraints:

* **Per-day time budget** — the durations of the activities placed on a day must not
  exceed the available sightseeing minutes for that day.
* **No overlaps** — no two activities on the same day may occupy the same time (CP-SAT
  interval variables + ``AddNoOverlap`` per day).
* **Opening hours where known** — when an activity carries parseable opening hours, it
  may only be placed inside that window; an activity with no (or unparseable) hours is
  unconstrained ("respected *where data exists*").
* **Weather (BS-074)** — outdoor activities may not be scheduled on a forecast
  weather-conflict day (technical-doc §12). When any day conflicts the scheduler
  **replans**: it re-solves against a weather-naive baseline and marks each item that
  the weather logic moved off a bad day, or swapped in to replace one, as
  :attr:`ScheduledActivity.was_rescheduled`.

Like the destination model this is deterministic and inspectable (no LLM), and it is
kept **separate** from destination selection so each concern is independently testable;
the pipeline composes them (schedule the activities of the chosen destination). The
per-activity ``weights`` argument is the objective seam — the value of doing each
activity (its preference/experience fit, BS-069) — so that when time is scarce the
scheduler keeps the *most valuable* activities rather than an arbitrary subset. Every
taken activity also carries a small base value, so the scheduler fills the available
time even when weights are equal or zero.

CP-SAT is integer-only, so time is modelled in **minutes from midnight** (a clock
timeline) and the objective weights are scaled to integers.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Sequence
from dataclasses import dataclass

from ortools.sat.python import cp_model

from ai_engine.core.optimizer import SolveStatus
from ai_engine.domain import Activity, IndoorOutdoor, WeatherProfile

logger = logging.getLogger(__name__)

_MINUTES_PER_DAY = 24 * 60

# A sightseeing day defaults to 09:00–21:00 (minutes from midnight). Activities are
# placed on this clock timeline; opening hours further restrict a given activity.
_DEFAULT_DAY_START = 9 * 60  # 09:00
_DEFAULT_DAY_END = 21 * 60  # 21:00
# Assumed visit length for an activity whose duration was never estimated (BS-065).
_DEFAULT_ACTIVITY_MINUTES = 120

# Objective weights (activity value, typically a [0, 1] score) scaled to integers.
_WEIGHT_SCALE = 1000
# Small guaranteed value per taken activity so the scheduler still fills available time
# when weights are equal or zero (it always prefers doing more over doing less).
_TAKE_BASE_VALUE = 1

# One HH:MM-HH:MM clock range (hyphen or en/em dash), anywhere in an OSM-style string.
_TIME_RANGE = re.compile(r"(\d{1,2}):(\d{2})\s*[-–—]\s*(\d{1,2}):(\d{2})")


def parse_opening_hours(text: str | None) -> tuple[tuple[int, int], ...] | None:
    """Parse opening hours into the open ``(open_minute, close_minute)`` windows, or ``None``.

    Handles the common OSM ``opening_hours`` shapes the POI sources emit — ``"24/7"``,
    ``"Mo-Su 09:00-18:00"``, a bare ``"09:00-17:00"`` — by extracting every
    ``HH:MM-HH:MM`` clock range. A day with a **lunch-break split**
    (``"08:00-12:00,13:00-17:00"``) returns *both* windows, so the scheduler can require
    an activity to fall entirely inside one of them rather than approximating the closed
    gap away. Windows are returned sorted and de-duplicated. Returns ``None`` when there
    is no data or nothing parseable — the caller then leaves the activity unconstrained
    ("opening hours respected *where available*"). Overnight or malformed ranges
    (close ≤ open) are ignored. Deterministic and pure.
    """
    if not text or not text.strip():
        return None
    lowered = text.lower()
    if "24/7" in lowered or "24/365" in lowered:
        return ((0, _MINUTES_PER_DAY),)
    ranges: set[tuple[int, int]] = set()
    for match in _TIME_RANGE.finditer(text):
        oh, om, ch, cm = (int(g) for g in match.groups())
        open_min = oh * 60 + om
        close_min = ch * 60 + cm
        if 0 <= open_min < close_min <= _MINUTES_PER_DAY:
            ranges.add((open_min, close_min))
    if not ranges:
        logger.debug("scheduler: no parseable opening hours in %r; leaving unconstrained", text)
        return None
    return tuple(sorted(ranges))


@dataclass(frozen=True)
class SchedulingPolicy:
    """The configurable day model for scheduling (all tunable, immutable).

    ``day_start_minute`` / ``day_end_minute`` bound the sightseeing timeline (minutes
    from midnight). ``available_minutes`` is the per-day activity time budget; it
    defaults to the full window length and may be set smaller to model a more relaxed
    pace. ``default_activity_minutes`` is used for activities whose duration was never
    estimated. ``transition_buffer_minutes`` is the travel/transition time assumed
    *between* consecutive activities on a day: it is charged against the day's time
    budget (each extra activity on a day consumes one buffer), so a packed day leaves
    room to actually move between sights. It is a per-day budget accounting, not a literal
    gap enforced between time slots; ``0`` (the default) keeps the original behaviour.
    """

    day_start_minute: int = _DEFAULT_DAY_START
    day_end_minute: int = _DEFAULT_DAY_END
    available_minutes: int | None = None
    default_activity_minutes: int = _DEFAULT_ACTIVITY_MINUTES
    transition_buffer_minutes: int = 0
    #: Hard cap on how many activities may be placed on a single day. ``None`` (the
    #: default) balances the load automatically — the cap becomes ``ceil(n / days)`` so
    #: activities spread evenly across the stay instead of piling onto one day (the
    #: time-budget constraint alone leaves the distribution arbitrary). Set an explicit
    #: value to model a fixed daily pace.
    max_activities_per_day: int | None = None
    max_time_seconds: float = 5.0

    def __post_init__(self) -> None:
        if not (0 <= self.day_start_minute < self.day_end_minute <= _MINUTES_PER_DAY):
            raise ValueError(
                "day window must satisfy 0 <= day_start_minute < day_end_minute <= 1440, "
                f"got [{self.day_start_minute}, {self.day_end_minute}]"
            )
        if self.default_activity_minutes < 1:
            raise ValueError(
                f"default_activity_minutes must be >= 1, got {self.default_activity_minutes!r}"
            )
        if self.transition_buffer_minutes < 0:
            raise ValueError(
                f"transition_buffer_minutes must be >= 0, got {self.transition_buffer_minutes!r}"
            )
        if self.max_activities_per_day is not None and self.max_activities_per_day < 1:
            raise ValueError(
                f"max_activities_per_day must be >= 1, got {self.max_activities_per_day!r}"
            )
        window = self.day_end_minute - self.day_start_minute
        if self.available_minutes is not None and not (1 <= self.available_minutes <= window):
            raise ValueError(
                f"available_minutes must be within [1, {window}] (the day window), "
                f"got {self.available_minutes!r}"
            )

    @property
    def budget_minutes(self) -> int:
        """The effective per-day time budget (explicit ``available_minutes`` or the window)."""
        if self.available_minutes is not None:
            return self.available_minutes
        return self.day_end_minute - self.day_start_minute


@dataclass(frozen=True)
class ScheduledActivity:
    """One activity placed on the itinerary: which day and clock window it occupies.

    ``was_rescheduled`` records that the weather logic moved this item off a
    weather-conflict day or swapped it in as a clear-day replacement (technical-doc
    §12; it maps to the ``trip_activities.was_rescheduled`` column). It is ``False``
    for an ordinary placement and whenever no weather conflict applied.
    """

    activity: Activity
    day: int  # 0-based day index within the stay
    start_minute: int  # clock minutes from midnight
    end_minute: int
    was_rescheduled: bool = False


@dataclass(frozen=True)
class Schedule:
    """The scheduling result: the placed activities plus any that did not fit."""

    status: SolveStatus
    items: tuple[ScheduledActivity, ...]
    unscheduled: tuple[Activity, ...]
    days: int

    @property
    def scheduled_count(self) -> int:
        return len(self.items)

    def by_day(self) -> list[list[ScheduledActivity]]:
        """The placed activities grouped per day (index ``d`` = day ``d``), each sorted by start."""
        grouped: list[list[ScheduledActivity]] = [[] for _ in range(self.days)]
        for item in self.items:
            grouped[item.day].append(item)
        for day_items in grouped:
            day_items.sort(key=lambda it: it.start_minute)
        return grouped


class ActivityScheduler:
    """Builds and solves the CP-SAT activity-scheduling model (technical-doc §15.1).

    Stateless apart from its :class:`SchedulingPolicy`; deterministic (single worker,
    fixed seed) so the same activities and days always yield the same itinerary.
    """

    def __init__(self, policy: SchedulingPolicy | None = None) -> None:
        self._policy = policy or SchedulingPolicy()

    def schedule(
        self,
        activities: Sequence[Activity],
        days: int,
        *,
        weights: Sequence[float] | None = None,
        weather_conflicts: Sequence[bool] | None = None,
    ) -> Schedule:
        """Select and place activities across ``days`` under the scheduling constraints.

        ``weights`` (optional) is the per-activity value used to prefer the most
        worthwhile activities when time is scarce; omitted means every activity is
        equally valuable (maximize how many are done).

        ``weather_conflicts`` (optional) is one flag per day (aligned to the day index,
        length ``days``) marking a day unfit for outdoor activities (WeatherAgent's
        deterministic conflict flag, BS-067/068). When any day conflicts the scheduler
        **replans** (technical-doc §12): outdoor activities are forbidden on conflict
        days, and each item that the weather logic moved off a bad day — or swapped in
        to replace one — is marked :attr:`ScheduledActivity.was_rescheduled`. Indoor and
        mixed activities are unaffected. Derive the flags from a
        :class:`~ai_engine.domain.WeatherProfile` with :func:`weather_conflict_flags`.

        Never raises on infeasible data: an activity that cannot fit any day (too long,
        an opening window narrower than its duration, or outdoor with every day a
        conflict) is simply returned unscheduled.
        """
        if days < 1:
            raise ValueError(f"days must be >= 1, got {days!r}")
        if weights is not None and len(weights) != len(activities):
            raise ValueError(
                f"weights length ({len(weights)}) must match activities ({len(activities)})"
            )
        if weather_conflicts is not None and len(weather_conflicts) != days:
            raise ValueError(
                f"weather_conflicts length ({len(weather_conflicts)}) must match days ({days})"
            )
        if not activities:
            return Schedule(status=SolveStatus.OPTIMAL, items=(), unscheduled=(), days=days)

        conflict_days = frozenset(d for d, c in enumerate(weather_conflicts or ()) if c)
        has_outdoor = any(a.indoor_outdoor == IndoorOutdoor.OUTDOOR for a in activities)
        # The weather constraint only bites when a conflict day and an outdoor activity
        # both exist; otherwise the reshuffle would be identical to the baseline.
        weather_active = bool(conflict_days) and has_outdoor

        # Weather-naive baseline: what the plan would be ignoring the forecast. This is
        # the reference the reschedule is measured against (technical-doc §12).
        base_status, base_place = self._solve_once(
            activities, days, weights, forbidden_outdoor_days=frozenset(), hint=None
        )

        if not weather_active:
            # No conflict, or nothing outdoor to move — the baseline is the final plan and
            # nothing was rescheduled (pass no conflict days so nothing is flagged).
            return self._build_schedule(activities, days, base_status, base_place, frozenset(), {})

        # Reshuffle: forbid outdoor activities on conflict days and re-solve, warm-started
        # from the baseline so unaffected items stay put and only the forced moves change.
        status, place = self._solve_once(
            activities, days, weights, forbidden_outdoor_days=conflict_days, hint=base_place
        )
        base_days = {ai: day for ai, (day, _s, _e) in base_place.items()}
        logger.info("scheduler: replanned around %d weather-conflict day(s)", len(conflict_days))
        return self._build_schedule(activities, days, status, place, conflict_days, base_days)

    def _solve_once(
        self,
        activities: Sequence[Activity],
        days: int,
        weights: Sequence[float] | None,
        *,
        forbidden_outdoor_days: frozenset[int],
        hint: dict[int, tuple[int, int, int]] | None,
    ) -> tuple[SolveStatus, dict[int, tuple[int, int, int]]]:
        """Build and solve one scheduling model; return the status and placement map.

        The placement maps an activity index to ``(day, start_minute, end_minute)`` for
        each scheduled activity. ``forbidden_outdoor_days`` are days on which outdoor
        activities may not be placed (the weather constraint); ``hint`` warm-starts the
        solver from a prior placement so a re-solve perturbs the plan minimally.
        """
        policy = self._policy
        budget = policy.budget_minutes
        buffer = policy.transition_buffer_minutes
        model = cp_model.CpModel()

        take: dict[int, cp_model.IntVar] = {}
        presence: dict[tuple[int, int], cp_model.IntVar] = {}
        starts: dict[tuple[int, int], cp_model.IntVar] = {}
        durations: dict[int, int] = {}
        intervals_by_day: list[list[cp_model.IntervalVar]] = [[] for _ in range(days)]

        for ai, activity in enumerate(activities):
            dur = activity.est_duration_minutes or policy.default_activity_minutes
            durations[ai] = dur
            take_var = model.NewBoolVar(f"take_{ai}")
            take[ai] = take_var
            is_outdoor = activity.indoor_outdoor == IndoorOutdoor.OUTDOOR

            # The open windows this activity's duration can actually fit into today,
            # from its opening hours (possibly several — a lunch-break split).
            windows = self._activity_windows(activity, dur)
            if dur > budget or not windows:
                # Cannot be placed on any day (too long, or no open window fits it).
                model.Add(take_var == 0)
                logger.debug(
                    "scheduler: %r cannot be placed (dur=%d, windows=%s, budget=%d)",
                    activity.name,
                    dur,
                    windows,
                    budget,
                )
                continue

            lo = min(w[0] for w in windows)
            hi = max(w[1] for w in windows)
            day_presence = []
            for d in range(days):
                pres = model.NewBoolVar(f"pres_{ai}_{d}")
                # Weather constraint: an outdoor activity may not sit on a conflict day.
                if is_outdoor and d in forbidden_outdoor_days:
                    model.Add(pres == 0)
                start = model.NewIntVar(lo, hi - dur, f"start_{ai}_{d}")
                end = model.NewIntVar(lo + dur, hi, f"end_{ai}_{d}")
                interval = model.NewOptionalIntervalVar(start, dur, end, pres, f"iv_{ai}_{d}")
                # With a lunch-break split, the visit must fall entirely inside *one* of
                # the open windows, not the closed gap between them.
                if len(windows) > 1:
                    selectors = []
                    for wi, (wlo, whi) in enumerate(windows):
                        sel = model.NewBoolVar(f"win_{ai}_{d}_{wi}")
                        model.Add(start >= wlo).OnlyEnforceIf(sel)
                        model.Add(start + dur <= whi).OnlyEnforceIf(sel)
                        selectors.append(sel)
                    model.Add(sum(selectors) == pres)
                intervals_by_day[d].append(interval)
                presence[(ai, d)] = pres
                starts[(ai, d)] = start
                day_presence.append(pres)
            # A taken activity is placed on exactly one day; an untaken one on none.
            model.Add(sum(day_presence) == take_var)

        # No two activities overlap in time on the same day.
        for day_intervals in intervals_by_day:
            if day_intervals:
                model.AddNoOverlap(day_intervals)

        # Per-day time budget: the placed durations, plus a transition buffer for each
        # activity beyond the first, must fit the budget. Charging (dur + buffer) and
        # allowing (budget + buffer) yields sum(dur) + (count - 1) * buffer <= budget.
        for d in range(days):
            terms = [
                presence[(ai, d)] * (durations[ai] + buffer)
                for ai in range(len(activities))
                if (ai, d) in presence
            ]
            if terms:
                model.Add(sum(terms) <= budget + buffer)

        # Per-day count cap so activities spread across the stay instead of clustering on
        # one day. Maximizing the number done leaves the day distribution arbitrary, so
        # without this the solver may pile everything onto a single day. The default cap
        # is ceil(n / days) — just enough to still fit every activity while forcing an
        # even spread — and an explicit policy value overrides it for a fixed daily pace.
        per_day_cap = policy.max_activities_per_day or -(-len(activities) // max(1, days))
        for d in range(days):
            day_presences = [
                presence[(ai, d)] for ai in range(len(activities)) if (ai, d) in presence
            ]
            if day_presences:
                model.Add(sum(day_presences) <= per_day_cap)

        # Objective: maximize the total value of the activities done (each taken one
        # also worth a small base value so the schedule fills available time).
        model.Maximize(
            sum(
                (_weight_units(weights[ai] if weights is not None else 0.0) + _TAKE_BASE_VALUE)
                * take[ai]
                for ai in take
            )
        )

        # Warm start from a prior placement so a re-solve keeps unaffected items put.
        if hint:
            for ai, (day, start_minute, _end) in hint.items():
                if (ai, day) in presence:
                    model.AddHint(presence[(ai, day)], 1)
                    model.AddHint(starts[(ai, day)], start_minute)
                    model.AddHint(take[ai], 1)

        solver = cp_model.CpSolver()
        solver.parameters.num_search_workers = 1  # deterministic
        solver.parameters.random_seed = 0
        solver.parameters.max_time_in_seconds = policy.max_time_seconds
        status = solver.Solve(model)

        if status not in (cp_model.OPTIMAL, cp_model.FEASIBLE):
            # Taking zero activities is always feasible, so this is defensive.
            logger.warning(
                "scheduler: solver returned %s for %d activit(y/ies) over %d day(s)",
                solver.StatusName(status),
                len(activities),
                days,
            )
            return SolveStatus.INFEASIBLE, {}

        placement: dict[int, tuple[int, int, int]] = {}
        for ai in range(len(activities)):
            placed_day = next(
                (d for d in range(days) if (ai, d) in presence and solver.Value(presence[(ai, d)])),
                None,
            )
            if placed_day is None:
                continue
            start_minute = int(solver.Value(starts[(ai, placed_day)]))
            placement[ai] = (placed_day, start_minute, start_minute + durations[ai])

        result_status = SolveStatus.OPTIMAL if status == cp_model.OPTIMAL else SolveStatus.FEASIBLE
        return result_status, placement

    def _activity_windows(self, activity: Activity, dur: int) -> list[tuple[int, int]]:
        """The open windows within the day that can fit ``dur`` for this activity.

        Clips the parsed opening-hours windows to the policy's day window and keeps only
        those wide enough for the visit. An activity with no (or unparseable) hours gets
        the full day window. An empty result means it cannot be placed at all.
        """
        policy = self._policy
        parsed = parse_opening_hours(activity.opening_hours)
        raw = parsed if parsed is not None else ((policy.day_start_minute, policy.day_end_minute),)
        clipped = ((max(policy.day_start_minute, o), min(policy.day_end_minute, c)) for o, c in raw)
        return [(lo, hi) for lo, hi in clipped if hi - lo >= dur]

    def _build_schedule(
        self,
        activities: Sequence[Activity],
        days: int,
        status: SolveStatus,
        placement: dict[int, tuple[int, int, int]],
        conflict_days: frozenset[int],
        base_days: dict[int, int],
    ) -> Schedule:
        """Assemble the :class:`Schedule` from a solved placement, flagging reschedules.

        An item is ``was_rescheduled`` when the weather logic caused it: an outdoor
        activity whose weather-naive baseline day was a conflict day (it had to move or
        drop), or an activity swapped in that the baseline had not scheduled at all.
        """
        if status is SolveStatus.INFEASIBLE:
            return Schedule(status=status, items=(), unscheduled=tuple(activities), days=days)

        items: list[ScheduledActivity] = []
        unscheduled: list[Activity] = []
        for ai, activity in enumerate(activities):
            if ai not in placement:
                unscheduled.append(activity)
                continue
            day, start_minute, end_minute = placement[ai]
            base_day = base_days.get(ai)
            outdoor = activity.indoor_outdoor == IndoorOutdoor.OUTDOOR
            was_rescheduled = bool(conflict_days) and (
                (outdoor and base_day is not None and base_day in conflict_days) or base_day is None
            )
            items.append(
                ScheduledActivity(
                    activity=activity,
                    day=day,
                    start_minute=start_minute,
                    end_minute=end_minute,
                    was_rescheduled=was_rescheduled,
                )
            )

        items.sort(key=lambda it: (it.day, it.start_minute))
        logger.info(
            "scheduler: placed %d/%d activit(y/ies) across %d day(s) (status=%s, rescheduled=%d)",
            len(items),
            len(activities),
            days,
            status.value,
            sum(1 for it in items if it.was_rescheduled),
        )
        return Schedule(
            status=status,
            items=tuple(items),
            unscheduled=tuple(unscheduled),
            days=days,
        )


def weather_conflict_flags(weather: WeatherProfile) -> list[bool]:
    """The per-day outdoor-conflict flags of a forecast, aligned to the stay's day index.

    A convenience bridge from the domain :class:`~ai_engine.domain.WeatherProfile`
    (BS-067) to the ``weather_conflicts`` argument of :meth:`ActivityScheduler.schedule`:
    returns ``[day.weather_conflict for day in weather.days]``. An empty (weather-
    unchecked) profile yields ``[]``, i.e. no constraint.
    """
    return [day.weather_conflict for day in weather.days]


def _weight_units(weight: float) -> int:
    """Scale a non-negative activity value to integer objective units (negatives floored to 0)."""
    return int(round(max(0.0, weight) * _WEIGHT_SCALE))
