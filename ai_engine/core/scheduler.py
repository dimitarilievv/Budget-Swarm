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
from ai_engine.domain import Activity

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


def parse_opening_hours(text: str | None) -> tuple[int, int] | None:
    """Parse opening hours into a ``(open_minute, close_minute)`` window, or ``None``.

    Handles the common OSM ``opening_hours`` shapes the POI sources emit — ``"24/7"``,
    ``"Mo-Su 09:00-18:00"``, a bare ``"09:00-17:00"`` — by extracting the ``HH:MM-HH:MM``
    clock ranges. Multiple ranges (e.g. a lunch-break split) are approximated by their
    **encompassing span** ``(earliest open, latest close)``: a single-window model
    cannot represent a midday gap, so this stays permissive rather than over-constrain.
    Returns ``None`` when there is no data or nothing parseable — the caller then leaves
    the activity unconstrained ("opening hours respected *where available*"). Overnight
    or malformed ranges (close ≤ open) are ignored. Deterministic and pure.
    """
    if not text or not text.strip():
        return None
    lowered = text.lower()
    if "24/7" in lowered or "24/365" in lowered:
        return (0, _MINUTES_PER_DAY)
    ranges: list[tuple[int, int]] = []
    for match in _TIME_RANGE.finditer(text):
        oh, om, ch, cm = (int(g) for g in match.groups())
        open_min = oh * 60 + om
        close_min = ch * 60 + cm
        if 0 <= open_min < close_min <= _MINUTES_PER_DAY:
            ranges.append((open_min, close_min))
    if not ranges:
        logger.debug("scheduler: no parseable opening hours in %r; leaving unconstrained", text)
        return None
    return (min(o for o, _ in ranges), max(c for _, c in ranges))


@dataclass(frozen=True)
class SchedulingPolicy:
    """The configurable day model for scheduling (all tunable, immutable).

    ``day_start_minute`` / ``day_end_minute`` bound the sightseeing timeline (minutes
    from midnight). ``available_minutes`` is the per-day activity time budget; it
    defaults to the full window length and may be set smaller to model a more relaxed
    pace. ``default_activity_minutes`` is used for activities whose duration was never
    estimated.
    """

    day_start_minute: int = _DEFAULT_DAY_START
    day_end_minute: int = _DEFAULT_DAY_END
    available_minutes: int | None = None
    default_activity_minutes: int = _DEFAULT_ACTIVITY_MINUTES
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
    """One activity placed on the itinerary: which day and clock window it occupies."""

    activity: Activity
    day: int  # 0-based day index within the stay
    start_minute: int  # clock minutes from midnight
    end_minute: int


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
    ) -> Schedule:
        """Select and place activities across ``days`` under the scheduling constraints.

        ``weights`` (optional) is the per-activity value used to prefer the most
        worthwhile activities when time is scarce; omitted means every activity is
        equally valuable (maximize how many are done). Never raises on infeasible data:
        an activity that cannot fit any day (too long, or an opening window narrower
        than its duration) is simply returned unscheduled.
        """
        if days < 1:
            raise ValueError(f"days must be >= 1, got {days!r}")
        if weights is not None and len(weights) != len(activities):
            raise ValueError(
                f"weights length ({len(weights)}) must match activities ({len(activities)})"
            )
        if not activities:
            return Schedule(status=SolveStatus.OPTIMAL, items=(), unscheduled=(), days=days)

        policy = self._policy
        budget = policy.budget_minutes
        model = cp_model.CpModel()

        take: dict[int, cp_model.IntVar] = {}
        # (activity_index, day) -> (presence bool, start var); intervals grouped per day.
        presence: dict[tuple[int, int], cp_model.IntVar] = {}
        starts: dict[tuple[int, int], cp_model.IntVar] = {}
        durations: dict[int, int] = {}
        intervals_by_day: list[list[cp_model.IntervalVar]] = [[] for _ in range(days)]

        for ai, activity in enumerate(activities):
            dur = activity.est_duration_minutes or policy.default_activity_minutes
            durations[ai] = dur
            take_var = model.NewBoolVar(f"take_{ai}")
            take[ai] = take_var

            # Earliest start / latest end for this activity within a day, tightened by
            # its opening hours when known.
            lo, hi = policy.day_start_minute, policy.day_end_minute
            window = parse_opening_hours(activity.opening_hours)
            if window is not None:
                lo = max(lo, window[0])
                hi = min(hi, window[1])

            if dur > budget or hi - lo < dur:
                # Cannot be placed on any day (too long, or opening window too small).
                model.Add(take_var == 0)
                logger.debug(
                    "scheduler: %r cannot be placed (dur=%d, window=[%d,%d], budget=%d)",
                    activity.name,
                    dur,
                    lo,
                    hi,
                    budget,
                )
                continue

            day_presence = []
            for d in range(days):
                pres = model.NewBoolVar(f"pres_{ai}_{d}")
                start = model.NewIntVar(lo, hi - dur, f"start_{ai}_{d}")
                end = model.NewIntVar(lo + dur, hi, f"end_{ai}_{d}")
                interval = model.NewOptionalIntervalVar(start, dur, end, pres, f"iv_{ai}_{d}")
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

        # Per-day time budget: the placed durations on a day must fit the budget.
        for d in range(days):
            terms = [
                presence[(ai, d)] * durations[ai]
                for ai in range(len(activities))
                if (ai, d) in presence
            ]
            if terms:
                model.Add(sum(terms) <= budget)

        # Objective: maximize the total value of the activities done (each taken one
        # also worth a small base value so the schedule fills available time).
        model.Maximize(
            sum(
                (_weight_units(weights[ai] if weights is not None else 0.0) + _TAKE_BASE_VALUE)
                * take[ai]
                for ai in take
            )
        )

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
            return Schedule(
                status=SolveStatus.INFEASIBLE,
                items=(),
                unscheduled=tuple(activities),
                days=days,
            )

        items: list[ScheduledActivity] = []
        unscheduled: list[Activity] = []
        for ai, activity in enumerate(activities):
            placed_day = next(
                (d for d in range(days) if (ai, d) in presence and solver.Value(presence[(ai, d)])),
                None,
            )
            if placed_day is None:
                unscheduled.append(activity)
                continue
            start_minute = int(solver.Value(starts[(ai, placed_day)]))
            items.append(
                ScheduledActivity(
                    activity=activity,
                    day=placed_day,
                    start_minute=start_minute,
                    end_minute=start_minute + durations[ai],
                )
            )

        items.sort(key=lambda it: (it.day, it.start_minute))
        result_status = SolveStatus.OPTIMAL if status == cp_model.OPTIMAL else SolveStatus.FEASIBLE
        logger.info(
            "scheduler: placed %d/%d activit(y/ies) across %d day(s) (status=%s)",
            len(items),
            len(activities),
            days,
            result_status.value,
        )
        return Schedule(
            status=result_status,
            items=tuple(items),
            unscheduled=tuple(unscheduled),
            days=days,
        )


def _weight_units(weight: float) -> int:
    """Scale a non-negative activity value to integer objective units (negatives floored to 0)."""
    return int(round(max(0.0, weight) * _WEIGHT_SCALE))
