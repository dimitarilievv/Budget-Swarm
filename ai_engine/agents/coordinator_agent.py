"""CoordinatorAgent — aggregate the per-candidate agent outputs (BS-071).

This is the coordinator's **first** responsibility (technical-doc §13, step 1):
turn the partial, differently-shaped results the parallel agents produced for one
candidate destination into a **single, validated structure** the scorer (§14) and
the optimizer (§15) can consume. Scoring, constraint enforcement and optimization
are *later* steps (BS-072 onward) and deliberately live elsewhere — this module
only assembles and annotates.

What it does, and nothing more:

* **Aggregate** the ScoutAgent destination together with its TransportAgent /
  StayAgent / ExperienceAgent / WeatherAgent outputs into one
  :class:`CandidateAggregate` per candidate.
* **Attach quality flags.** Every domain model already carries a
  :class:`~ai_engine.domain.QualityFlag`; the coordinator surfaces a per-part
  summary (:attr:`CandidateAggregate.quality_flags`) and the single worst present
  flag (:attr:`CandidateAggregate.overall_quality`) so the honesty of each number
  propagates outward to the optimizer and the UI (technical-doc §16/§22/§26).
* **Mark missing / degraded pieces.** "Degrade, don't crash" (ADR-005,
  technical-doc §22): a candidate whose transport, lodging, activities or weather
  never arrived is *not* dropped — the missing part is named in
  :attr:`CandidateAggregate.degraded` so scoring can penalize it and the UI can be
  transparent, rather than the whole request failing.

Two hard boundaries from the ticket:

* **Consumes only domain models.** The inputs are exclusively the value objects in
  :mod:`ai_engine.domain`; the coordinator has **no provider knowledge** — it never
  sees a Travelpayouts record, an Inside Airbnb row or an Open-Meteo payload, only
  the normalized shapes the agents already mapped them to (technical-doc §17/§21).
* **Deterministic and pure.** No I/O, no randomness: the same inputs always yield
  the same aggregate, which is what makes the decision core defensible as an
  optimization system (technical-doc §13) and trivially unit-testable.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field

from ai_engine.core.scorer import CandidateInputs
from ai_engine.domain import (
    Accommodation,
    Activity,
    Destination,
    QualityFlag,
    TransportOption,
    WeatherProfile,
)

logger = logging.getLogger(__name__)

# Canonical names of the aggregate's parts. ``DESTINATION`` is always present (it is
# the candidate itself); the other four come from the parallel agents and may be
# missing. These names are what :attr:`CandidateAggregate.quality_flags` is keyed on
# and what :attr:`CandidateAggregate.degraded` reports, so callers (scorer, optimizer,
# UI) refer to parts by a stable string rather than re-deriving them.
PART_DESTINATION = "destination"
PART_TRANSPORT = "transport"
PART_ACCOMMODATION = "accommodation"
PART_ACTIVITIES = "activities"
PART_WEATHER = "weather"

# Worst-to-best severity of a quality flag, so the aggregate can report the single
# firmest-common denominator: an aggregate is only as trustworthy as its weakest
# present piece. ``estimated`` is the least firm, ``real`` the most (enums §26).
_QUALITY_SEVERITY: Mapping[QualityFlag, int] = {
    QualityFlag.REAL: 0,
    QualityFlag.CACHED: 1,
    QualityFlag.ESTIMATED: 2,
}


def _worst_flag(flags: Sequence[QualityFlag]) -> QualityFlag | None:
    """The least-firm flag in ``flags`` (``estimated`` beats ``cached`` beats ``real``).

    Returns ``None`` for an empty sequence — the caller treats "no present flag" as a
    missing part rather than a quality level.
    """
    if not flags:
        return None
    return max(flags, key=lambda f: _QUALITY_SEVERITY[f])


def _collection_flag(items: Sequence[Accommodation] | Sequence[Activity]) -> QualityFlag | None:
    """The worst ``quality_flag`` across a collection of domain models.

    Used for the accommodations / activities lists: the list is only as firm as its
    least-firm member. Empty -> ``None`` (the part is missing, reported via degraded).
    """
    return _worst_flag([item.quality_flag for item in items])


@dataclass(frozen=True)
class AgentOutputs:
    """The raw per-candidate results the pipeline collected from the parallel agents.

    A loose input bundle — exclusively :mod:`ai_engine.domain` value objects, with
    ``None`` / empty allowed for any agent that failed or returned nothing (§18/§22).
    The coordinator turns one of these into a validated :class:`CandidateAggregate`.
    ``nights`` is the number of paid nights of the stay (from the request duration),
    carried through so the scorer can cost lodging and size the activity target; it is
    an engine-native scalar, not provider data.
    """

    destination: Destination
    transport: TransportOption | None = None
    accommodations: Sequence[Accommodation] = ()
    activities: Sequence[Activity] = ()
    weather: WeatherProfile | None = None
    nights: int = 1


@dataclass(frozen=True)
class CandidateAggregate:
    """One candidate's aggregated, quality-flagged agent outputs (the §13 step-1 result).

    The single structure the scorer and optimizer consume. It holds the normalized
    domain pieces plus two provenance summaries the coordinator derives:

    * :attr:`quality_flags` — a per-present-part map to its
      :class:`~ai_engine.domain.QualityFlag` (including the destination), so honesty
      about each piece is explicit rather than buried inside the nested models.
    * :attr:`degraded` — the sorted names of parts that are **missing or unchecked**
      (no transport, no lodging, no activities, or a weather profile that never
      resolved). Present so downstream stages can penalize/label a thin candidate
      without dropping it ("degrade, don't crash").

    Collections are frozen to tuples so the value object stays immutable; ``nights``
    is validated ``>= 1`` to match the scorer's :class:`CandidateInputs` contract.
    """

    destination: Destination
    transport: TransportOption | None
    accommodations: tuple[Accommodation, ...]
    activities: tuple[Activity, ...]
    weather: WeatherProfile | None
    nights: int
    quality_flags: Mapping[str, QualityFlag] = field(default_factory=dict)
    degraded: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if self.nights < 1:
            raise ValueError(f"nights must be >= 1, got {self.nights!r}")
        # Freeze the mutable-at-the-boundary containers so the aggregate is a true
        # immutable value object (hashable collections, read-only mapping view).
        object.__setattr__(self, "accommodations", tuple(self.accommodations))
        object.__setattr__(self, "activities", tuple(self.activities))
        object.__setattr__(self, "degraded", tuple(self.degraded))
        object.__setattr__(self, "quality_flags", dict(self.quality_flags))

    @property
    def is_complete(self) -> bool:
        """True when every agent part arrived — no missing/unchecked pieces."""
        return not self.degraded

    @property
    def overall_quality(self) -> QualityFlag:
        """The single worst quality flag across all present parts.

        The aggregate is only as firm as its least-firm present piece, so this is the
        honest headline flag to propagate to the optimizer/UI. The destination is
        always present, so there is always at least one flag to reduce over.
        """
        worst = _worst_flag(list(self.quality_flags.values()))
        # The destination flag is always recorded, so ``worst`` is never None here;
        # fall back defensively to the destination's own flag just in case.
        return worst if worst is not None else self.destination.quality_flag

    @property
    def scorer_inputs(self) -> CandidateInputs:
        """Adapt this aggregate to the scorer's :class:`CandidateInputs` (BS-069).

        The bridge into scoring (technical-doc §14): the scorer needs exactly the
        normalized pieces gathered here. Provenance (``quality_flags`` / ``degraded``)
        is intentionally *not* passed — the scorer already penalizes a missing piece
        via its own neutral/zero-score rules; the flags travel alongside for the
        optimizer and the UI instead.
        """
        return CandidateInputs(
            features=self.destination.features,
            transport=self.transport,
            accommodations=self.accommodations,
            activities=self.activities,
            weather=self.weather,
            nights=self.nights,
        )


class CoordinatorAgent:
    """Aggregates each candidate's agent outputs into a scorer/optimizer-ready structure.

    Stateless and deterministic. This is only the coordinator's **aggregation** step
    (technical-doc §13, step 1); scoring, constraint enforcement and optimization are
    separate, later components.
    """

    def aggregate(self, outputs: AgentOutputs) -> CandidateAggregate:
        """Aggregate one candidate's collected agent outputs into a single structure.

        Records a :class:`~ai_engine.domain.QualityFlag` for every part that arrived
        and names every part that did not (or that resolved to nothing usable — an
        empty lodging/activity list, or a weather profile with no forecast days, i.e.
        weather-unchecked). Never raises on a missing part: a thin candidate degrades,
        it does not fail.
        """
        quality_flags: dict[str, QualityFlag] = {
            PART_DESTINATION: outputs.destination.quality_flag
        }
        degraded: list[str] = []

        # Transport — a single option (or missing).
        if outputs.transport is not None:
            quality_flags[PART_TRANSPORT] = outputs.transport.quality_flag
        else:
            degraded.append(PART_TRANSPORT)

        # Accommodations / activities — collections; present iff non-empty.
        accommodations = tuple(outputs.accommodations)
        acc_flag = _collection_flag(accommodations)
        if acc_flag is not None:
            quality_flags[PART_ACCOMMODATION] = acc_flag
        else:
            degraded.append(PART_ACCOMMODATION)

        activities = tuple(outputs.activities)
        act_flag = _collection_flag(activities)
        if act_flag is not None:
            quality_flags[PART_ACTIVITIES] = act_flag
        else:
            degraded.append(PART_ACTIVITIES)

        # Weather — the agent always returns a profile, but an empty one means the
        # forecast could not be obtained (weather-unchecked), which is a degraded part.
        if outputs.weather is not None and outputs.weather.days:
            quality_flags[PART_WEATHER] = outputs.weather.quality_flag
        else:
            degraded.append(PART_WEATHER)

        aggregate = CandidateAggregate(
            destination=outputs.destination,
            transport=outputs.transport,
            accommodations=accommodations,
            activities=activities,
            weather=outputs.weather,
            nights=outputs.nights,
            quality_flags=quality_flags,
            degraded=tuple(degraded),
        )
        if degraded:
            logger.info(
                "coordinator: %s/%s aggregated with degraded parts %s (overall quality=%s)",
                outputs.destination.city,
                outputs.destination.country,
                list(degraded),
                aggregate.overall_quality.value,
            )
        else:
            logger.debug(
                "coordinator: %s/%s aggregated complete (overall quality=%s)",
                outputs.destination.city,
                outputs.destination.country,
                aggregate.overall_quality.value,
            )
        return aggregate

    def aggregate_all(self, outputs: Sequence[AgentOutputs]) -> list[CandidateAggregate]:
        """Aggregate every candidate's outputs, preserving the input (Scout) order.

        The batch entry point the pipeline uses after the per-candidate fan-out
        (technical-doc §18). Order is preserved so the ranked candidate list stays
        aligned across the scorer and optimizer.
        """
        aggregates = [self.aggregate(o) for o in outputs]
        degraded_count = sum(1 for a in aggregates if a.degraded)
        logger.debug(
            "coordinator: aggregated %d candidate(s), %d with degraded parts",
            len(aggregates),
            degraded_count,
        )
        return aggregates
