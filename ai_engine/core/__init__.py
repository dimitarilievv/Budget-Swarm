"""Engine reasoning core: pipeline orchestration, scoring, optimization, explanation.

The full engine is wired: :class:`TripPlanEngine` runs Scout → parallel agents →
coordinator → scorer → optimizer → explanation and returns the final structured plan.
Re-exported here so callers can ``from ai_engine.core import TripPlanEngine, EngineInput,
Planner, ...``.
"""

from __future__ import annotations

from .engine import TripPlanEngine
from .explanation import explain_plan, explanation_input_from_plan
from .optimizer import (
    ExclusionReason,
    OptimizationInput,
    OptimizationResult,
    Optimizer,
    SolveStatus,
)
from .pipeline import (
    DEFAULT_AGENT_CONCURRENCY,
    AgentSet,
    EngineInput,
    gather_agent_outputs,
)
from .planner import (
    NoPlanAvailableError,
    OptimalPlan,
    Planner,
    PlanRequest,
    RankedCandidate,
)
from .scheduler import (
    ActivityScheduler,
    Schedule,
    ScheduledActivity,
    SchedulingPolicy,
    parse_opening_hours,
    weather_conflict_flags,
)

__all__ = [
    "TripPlanEngine",
    "EngineInput",
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
    "Planner",
    "PlanRequest",
    "OptimalPlan",
    "RankedCandidate",
    "NoPlanAvailableError",
    "explain_plan",
    "explanation_input_from_plan",
    "AgentSet",
    "gather_agent_outputs",
    "DEFAULT_AGENT_CONCURRENCY",
]
