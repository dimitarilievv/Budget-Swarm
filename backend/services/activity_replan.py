"""Re-plan a saved trip's day-by-day activities against a refreshed forecast.

When the weather for a saved (often reserved/locked) trip changes, the itinerary that was
scheduled against the old forecast can become inconsistent — an outdoor stop left on a day
that's now rainy. This re-runs **only** phase-2 scheduling (the engine's public
:class:`~ai_engine.core.scheduler.ActivityScheduler`) over the trip's existing activities,
keeping the destination / transport / stay fixed, so outdoor activities move off the new
weather-conflict days (and drop if nothing fits). It does **not** pull in new alternatives
from the destination's wider POI pool — it rearranges the activities already in the plan.

The engine is imported lazily so this module stays importable in the stub-only deployment,
and the operation is behind a seam (``get_activity_replanner``) so tests can stub it. It
never raises: any failure degrades to "no change" (``None``), leaving the saved plan intact.
"""

from __future__ import annotations

import logging
from typing import Protocol, runtime_checkable

from schemas.trip_schema import (
    ItineraryActivity,
    ItineraryDay,
    PlanWeatherDay,
    TripResponse,
)

logger = logging.getLogger(__name__)


def _hhmm(minutes: int) -> str:
    return f"{minutes // 60:02d}:{minutes % 60:02d}"


def _to_minutes(value: str | None) -> int | None:
    if not value:
        return None
    try:
        h, m = value.split(":")
        return int(h) * 60 + int(m)
    except (ValueError, AttributeError):
        return None


def _conflict_flags(weather: list[PlanWeatherDay], days: int) -> list[bool]:
    flags = [bool(d.weather_conflict) for d in weather][:days]
    flags += [False] * (days - len(flags))
    return flags


@runtime_checkable
class ActivityReplanner(Protocol):
    """Re-schedule a plan's activities against new weather → a new itinerary, or ``None``.

    Returns ``None`` when nothing needs to change (the conflict days are unchanged) or when
    re-planning isn't possible, so the caller keeps the existing itinerary.
    """

    def replan(
        self, response: TripResponse, weather: list[PlanWeatherDay], days: int
    ) -> list[ItineraryDay] | None: ...


class NullActivityReplanner:
    """No-op re-planner (the default when the engine isn't wired)."""

    def replan(
        self, response: TripResponse, weather: list[PlanWeatherDay], days: int
    ) -> list[ItineraryDay] | None:
        return None


class EngineActivityReplanner:
    """Re-schedules via the engine's CP-SAT :class:`ActivityScheduler` (public API)."""

    def replan(
        self, response: TripResponse, weather: list[PlanWeatherDay], days: int
    ) -> list[ItineraryDay] | None:
        plan = response.chosen_plan
        current = [a for day in plan.itinerary for a in day.activities]
        if not current or days < 1:
            return None

        old_conflicts = _conflict_flags(plan.weather, days)
        new_conflicts = _conflict_flags(weather, days)
        if old_conflicts == new_conflicts:
            return None  # the forecast's conflict pattern didn't change — nothing to re-plan

        try:
            from ai_engine.core.scheduler import ActivityScheduler
            from ai_engine.domain.enums import IndoorOutdoor
            from ai_engine.domain.models import Activity

            def _activity(a: ItineraryActivity) -> Activity:
                start, end = _to_minutes(a.start_time), _to_minutes(a.end_time)
                dur = end - start if start is not None and end is not None and end > start else None
                return Activity(
                    name=a.name,
                    category=a.category,
                    latitude=a.latitude,
                    longitude=a.longitude,
                    opening_hours=a.opening_hours,
                    indoor_outdoor=IndoorOutdoor(a.indoor_outdoor),
                    est_duration_minutes=dur,
                )

            activities = [_activity(a) for a in current]
            schedule = ActivityScheduler().schedule(
                activities, days, weather_conflicts=new_conflicts
            )
        except Exception as exc:  # noqa: BLE001 - re-plan must never break the refresh flow
            logger.warning("activity_replan: scheduling failed (%s); keeping the itinerary", exc)
            return None

        if not schedule.items:
            # Everything dropped (e.g. all-outdoor on all-conflict days) — don't wipe the plan.
            return None

        by_day: dict[int, list] = {}
        for item in schedule.items:
            by_day.setdefault(item.day, []).append(item)
        itinerary: list[ItineraryDay] = []
        for day in sorted(by_day):
            items = sorted(by_day[day], key=lambda it: it.start_minute)
            itinerary.append(
                ItineraryDay(
                    day_number=day + 1,
                    activities=[
                        ItineraryActivity(
                            name=it.activity.name,
                            category=it.activity.category,
                            indoor_outdoor=it.activity.indoor_outdoor.value,
                            start_time=_hhmm(it.start_minute),
                            end_time=_hhmm(it.end_minute),
                            was_rescheduled=it.was_rescheduled,
                            latitude=it.activity.latitude,
                            longitude=it.activity.longitude,
                            opening_hours=it.activity.opening_hours,
                        )
                        for it in items
                    ],
                )
            )
        logger.info("activity_replan: re-scheduled %d day(s) around new weather", len(itinerary))
        return itinerary
