"""Engine reasoning core: pipeline orchestration, scoring, and optimization.

Only the pipeline entry point is implemented so far (a stub, BS-044); ``scorer``
and ``optimizer`` arrive with their own tickets. Re-exported here so callers can
``from ai_engine.core import run, EngineInput, TripPlan``.
"""

from __future__ import annotations

from .pipeline import EngineInput, TripPlan, run

__all__ = ["run", "EngineInput", "TripPlan"]
