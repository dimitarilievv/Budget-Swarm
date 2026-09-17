"""Explanation integration — turn an :class:`OptimalPlan` into a narration (Phase 7).

The optimizer has already made and verified the decision (Phase 6); this module is the
bridge that hands the plan's *facts* to the :class:`~ai_engine.providers.explanation.
ExplanationService` so it can produce the human-readable justification the API returns
(technical-doc §16).

Two guarantees, straight from §16:

* **The LLM cannot change the decision.** This module builds a fact-only
  :class:`~ai_engine.providers.explanation.TripExplanationInput` from an
  *already-chosen* :class:`OptimalPlan` and asks the service to *narrate* it. The plan
  is a frozen value object and is never fed back into or mutated by the explanation
  step — the returned :class:`~ai_engine.providers.explanation.Explanation` is only
  text. Even an LLM that hallucinates a different city changes nothing about the plan.
* **A templated fallback always produces output.** The service degrades to the
  deterministic templated backend whenever no LLM is configured or the LLM errors, so
  a request never fails for lack of an explanation (the LLM is non-critical, §16.4).

The bridge needs the originating :class:`~ai_engine.core.planner.PlanRequest` as well
as the plan, because the explanation restates the user's *budget*, *preferences* and
*trip length* — context the plan itself does not carry. It only ever restates facts
already computed; it derives no new numbers.
"""

from __future__ import annotations

from collections.abc import Mapping

from ai_engine.core.optimizer import ExclusionReason
from ai_engine.core.planner import OptimalPlan, PlanRequest
from ai_engine.providers.explanation import (
    Explanation,
    ExplanationService,
    TripExplanationInput,
)

# How an excluded runner-up is described — honest about *why* an otherwise-appealing
# alternative was not chosen (mirrors the §16.3 "exceeded the budget" example).
_EXCLUSION_PHRASE: Mapping[ExclusionReason, str] = {
    ExclusionReason.OVER_BUDGET: "scored well but came in over your budget",
    ExclusionReason.MODE_MISMATCH: "did not match your chosen transport mode",
    ExclusionReason.DURATION_MISMATCH: "did not match your requested trip length",
    ExclusionReason.MISSING_TRANSPORT: "had no costed way to get there",
    ExclusionReason.COST_UNKNOWN: "could not be fully costed",
}


def _positive_preferences(preferences: Mapping[str, float]) -> list[str]:
    """The user's preference keywords with a positive weight, in their given order."""
    return [k for k, w in preferences.items() if isinstance(w, (int, float)) and w > 0]


def _runner_up(plan: OptimalPlan) -> tuple[str | None, str | None]:
    """The most notable alternative to the chosen destination, and why it lost.

    Uses the ranked candidates (feasible first, by score): the top entry that is not the
    chosen destination. A feasible runner-up "was the next-best option within budget"; an
    excluded one carries the reason it was ruled out. ``(None, None)`` when there is no
    other candidate.
    """
    others = [rc for rc in plan.ranked_candidates if rc.destination.city != plan.destination.city]
    if not others:
        return None, None
    rc = others[0]
    if rc.feasible:
        return rc.destination.city, "was the next-best option within budget"
    note = _EXCLUSION_PHRASE.get(rc.exclusion_reason) if rc.exclusion_reason else None
    return rc.destination.city, note or "was not feasible under your constraints"


def _weather_note(plan: OptimalPlan) -> str | None:
    """A factual note about how the forecast was handled, or ``None`` when unremarkable.

    Only restates what actually happened: a reschedule when the weather logic moved an
    outdoor activity, otherwise a supportive note when the forecast is clear. Says
    nothing when there is no forecast (weather-unchecked).
    """
    if any(item.was_rescheduled for item in plan.itinerary):
        return "its forecast weather conflict was handled by rescheduling outdoor activities"
    weather = plan.weather
    if weather is not None and weather.days and not any(d.weather_conflict for d in weather.days):
        return "its forecast supports the planned outdoor sightseeing"
    return None


def explanation_input_from_plan(
    plan: OptimalPlan, request: PlanRequest, *, currency: str = "EUR"
) -> TripExplanationInput:
    """Map a chosen :class:`OptimalPlan` (+ its request) to the explanation's facts DTO.

    Restates only already-computed facts — destination, budget vs. realized total,
    trip length, matched preferences, the notable runner-up and a weather note. Pure and
    deterministic; computes no new numbers.
    """
    runner_up, runner_up_note = _runner_up(plan)
    return TripExplanationInput(
        destination=plan.destination.city,
        country=plan.destination.country,
        budget=request.budget,
        total_cost=plan.total_cost,
        currency=currency,
        # The template renders this as "nights"; use the billed lodging nights.
        duration_days=request.nights,
        preferences=_positive_preferences(request.preferences),
        runner_up=runner_up,
        runner_up_note=runner_up_note,
        weather_note=_weather_note(plan),
    )


def explain_plan(
    plan: OptimalPlan,
    request: PlanRequest,
    service: ExplanationService | None = None,
    *,
    currency: str = "EUR",
) -> Explanation:
    """Produce the explanation for a chosen plan via the service (templated by default).

    Builds the fact-only input from the plan and asks ``service`` to narrate it. With no
    ``service`` (or a templated-only one) a deterministic sentence is returned; with an
    LLM primary that fails, the service degrades to the same templated text. The plan is
    never modified — the LLM cannot change the decision, only describe it.
    """
    service = service or ExplanationService()
    return service.explain(explanation_input_from_plan(plan, request, currency=currency))
