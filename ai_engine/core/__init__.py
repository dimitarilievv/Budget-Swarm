"""Engine reasoning core: pipeline orchestration, scoring, and optimization.

The pipeline entry point (a stub, BS-044) and the CP-SAT optimizer (BS-072) are
implemented; the scorer lives alongside in ``scorer``. Re-exported here so callers
can ``from ai_engine.core import run, EngineInput, TripPlan, Optimizer, ...``.
"""

from __future__ import annotations

from .optimizer import (
    ExclusionReason,
    OptimizationInput,
    OptimizationResult,
    Optimizer,
    SolveStatus,
)
from .pipeline import EngineInput, TripPlan, run
from .scheduler import (
    ActivityScheduler,
    Schedule,
    ScheduledActivity,
    SchedulingPolicy,
    parse_opening_hours,
    weather_conflict_flags,
)

__all__ = [
    "run",
    "EngineInput",
    "TripPlan",
    "Optimizer",
    "OptimizationInput",
    "OptimizationResult",
    "SolveStatus",
    "ExclusionReason",
    "ActivityScheduler",
    "Schedule",
    "ScheduledActivity",
    "SchedulingPolicy",
    "parse_opening_hours",
    "weather_conflict_flags",
]
