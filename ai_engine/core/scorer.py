"""Scoring: per-candidate-set normalization (BS-068) + factor scores (BS-069).

The scorer turns each candidate destination's raw factors — cost, travel time,
culture/nature/food scores, weather, … — into a small set of comparable numbers
(technical-doc §14). It has two layers:

1. **Normalization** (BS-068) — min-max scaling computed per request's candidate
   set, so factors on wildly different scales (euros vs. minutes vs. a 0–1 index)
   land on one common ``[0, 1]`` scale.
2. **Factor scores** (BS-069) — six deterministic per-candidate factors
   (cost, preference, experience, weather, travel, accommodation), each in
   ``[0, 1]`` and oriented so **bigger is always better**. BS-070 weights and sums
   them into one overall score.

Two things make the normalization more than a textbook min-max:

* **Direction.** For a "higher is better" factor (a culture score) the largest raw
  value should map to ``1.0``; for a "lower is better" factor (cost, travel time)
  the *smallest* raw value should map to ``1.0``. Pass ``invert=True`` (or
  :data:`LOWER_IS_BETTER`) for the latter.
* **Degenerate sets.** A single candidate, or a set whose values are all equal, has
  a zero range — ``(x - min) / (max - min)`` divides by zero. That is not an error
  here (a one-city request is normal): every such candidate is given the same
  neutral :data:`DEGENERATE_VALUE`, so the factor simply stops differentiating
  candidates instead of crashing.

Missing data is handled explicitly: a ``None`` value is excluded from the min/max
range and assigned :data:`MISSING_VALUE` — ``0.0`` by default, so unknown data is
penalized rather than silently rewarded.

Everything here is pure and deterministic — no I/O, no randomness — and depends only
on the domain models (BS-038), so it is trivially unit-testable and reusable by the
coordinator/optimizer.
"""

from __future__ import annotations

import logging
import statistics
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field, replace

from ai_engine.domain import (
    Accommodation,
    Activity,
    DestinationFeatures,
    TransportOption,
    WeatherProfile,
)

logger = logging.getLogger(__name__)

# Value given to every candidate when a factor cannot differentiate them (a single
# candidate, or a zero-range set). 1.0 treats them all as equally best; because it is
# constant across the set it never changes the ranking, only the absolute score.
DEGENERATE_VALUE = 1.0

# Value given to a candidate whose raw factor is missing (``None``). 0.0 penalizes
# unknown data — an un-enriched candidate is treated as worst on that factor rather
# than being rewarded for the gap.
MISSING_VALUE = 0.0

# Readable aliases for the ``invert`` flag at call sites.
HIGHER_IS_BETTER = False
LOWER_IS_BETTER = True


def minmax_normalize(
    values: Sequence[float | None],
    *,
    invert: bool = False,
    degenerate_value: float = DEGENERATE_VALUE,
    missing_value: float = MISSING_VALUE,
) -> list[float]:
    """Min-max normalize ``values`` to ``[0, 1]``, aligned to the input order.

    Each present (non-``None``) value ``x`` maps to ``(x - lo) / (hi - lo)``, so the
    set's minimum becomes ``0.0`` and its maximum ``1.0``. With ``invert=True`` the
    scale is flipped (``(hi - x) / (hi - lo)``) so the *smallest* raw value scores
    ``1.0`` — use it for cost/time, where lower is better.

    Edge cases, all non-fatal:

    * empty input → ``[]``;
    * every value ``None`` → ``missing_value`` for each;
    * a single value, or a zero range (all present values equal) → ``degenerate_value``
      for each present value (``missing_value`` for any ``None``);
    * individual ``None`` values → ``missing_value``, and they do not affect the range.

    Results are clamped into ``[0, 1]`` defensively.
    """
    present = [v for v in values if v is not None]
    if not values:
        return []
    if not present:
        logger.debug("normalize: all %d values missing; using missing_value", len(values))
        return [missing_value] * len(values)

    lo, hi = min(present), max(present)
    span = hi - lo
    if span <= 0:
        # Single candidate or every value identical — the factor cannot rank them.
        logger.debug(
            "normalize: degenerate range (lo==hi==%s) over %d value(s); using degenerate_value",
            lo,
            len(present),
        )
        return [missing_value if v is None else degenerate_value for v in values]

    normalized: list[float] = []
    for v in values:
        if v is None:
            normalized.append(missing_value)
            continue
        scaled = (v - lo) / span
        if invert:
            scaled = 1.0 - scaled
        normalized.append(_clamp01(scaled))
    return normalized


def normalize_factors(
    columns: Mapping[str, Sequence[float | None]],
    *,
    lower_is_better: Sequence[str] = (),
    degenerate_value: float = DEGENERATE_VALUE,
    missing_value: float = MISSING_VALUE,
) -> dict[str, list[float]]:
    """Normalize several factor columns for one candidate set in a single call.

    ``columns`` maps a factor name to its raw values across the candidates (each
    sequence the same length and in candidate order). Every column is min-max
    normalized independently; columns named in ``lower_is_better`` (cost, travel
    time, …) are inverted so that — as everywhere else — bigger normalized means
    better. Returns a dict of the same keys to normalized lists.
    """
    invert_set = set(lower_is_better)
    return {
        name: minmax_normalize(
            values,
            invert=name in invert_set,
            degenerate_value=degenerate_value,
            missing_value=missing_value,
        )
        for name, values in columns.items()
    }


def _clamp01(value: float) -> float:
    """Clamp a float into ``[0.0, 1.0]`` (guards against float drift at the edges)."""
    if value < 0.0:
        return 0.0
    if value > 1.0:
        return 1.0
    return value


# ============================================================================
# Factor scores (BS-069) — six deterministic per-candidate factors in [0, 1]
# ============================================================================
#
# The scorer reduces each candidate to six comparable factor scores (technical-doc
# §14), every one in [0, 1] where bigger is better:
#
#   cost          cheaper total trip        (normalized across the set, inverted)
#   preference    features fit the user     (intrinsic weighted average)
#   experience    activities fill the days  (intrinsic coverage)
#   weather       few weather-conflict days (intrinsic non-conflict fraction)
#   travel        shorter journey           (normalized across the set, inverted)
#   accommodation well-rated places to stay (intrinsic best-rating)
#
# cost and travel are magnitudes on open scales, so they are min-max normalized
# across the request's candidate set; the other four are intrinsically bounded and
# computed per candidate. Every function is pure and deterministic.

# Neutral weather score when the forecast could not be obtained (weather-unchecked).
WEATHER_UNCHECKED_SCORE = 0.5
# Score when lodging options exist but none carry a rating (availability known,
# quality unknown).
UNRATED_ACCOMMODATION_SCORE = 0.5
# Assumed sightseeing activities filled per day, for experience coverage.
DEFAULT_ACTIVITIES_PER_DAY = 2
_MAX_RATING = 5.0

# Preference keyword -> the DestinationFeatures attribute it scores against.
_PREFERENCE_FEATURES = {
    "culture": "culture_score",
    "nature": "nature_score",
    "nightlife": "nightlife_score",
    "food": "food_score",
    "tourism": "tourism_score",
    "season": "season_score",
}

# A user's relative interest per preference keyword (non-negative; ratios only).
PreferenceWeights = Mapping[str, float]


@dataclass(frozen=True)
class CandidateInputs:
    """One candidate's normalized agent outputs, the raw material for its factors.

    Assembled by the coordinator from the per-candidate agent results. Any field may
    be absent (``None`` / empty) — the factor functions degrade to a neutral or
    penalized score rather than failing.
    """

    features: DestinationFeatures | None = None
    transport: TransportOption | None = None
    accommodations: Sequence[Accommodation] = field(default_factory=tuple)
    activities: Sequence[Activity] = field(default_factory=tuple)
    weather: WeatherProfile | None = None
    nights: int = 1

    def __post_init__(self) -> None:
        if self.nights < 1:
            raise ValueError(f"nights must be >= 1, got {self.nights!r}")


@dataclass(frozen=True)
class FactorScores:
    """The six deterministic factor scores for one candidate, each in ``[0, 1]``."""

    cost: float = 0.0
    preference: float = 0.0
    experience: float = 0.0
    weather: float = 0.0
    travel: float = 0.0
    accommodation: float = 0.0


# --- intrinsic per-candidate factors (already bounded in [0, 1]) ------------


def preference_score(features: DestinationFeatures | None, weights: PreferenceWeights) -> float:
    """Weighted average of the destination's feature scores under the user's weights.

    ``sum(weight_f * feature_f) / sum(weight_f)`` over the features the destination
    actually has (each feature is already a 0-1 index). A missing feature is dropped
    from *both* sums rather than counted as zero, so an un-enriched field does not
    unfairly sink the score. Returns ``0.0`` when there are no features, no positive
    weights, or no overlap. Deterministic.
    """
    if features is None:
        return 0.0
    weighted = 0.0
    total = 0.0
    for key, weight in weights.items():
        if weight <= 0:
            continue
        attr = _PREFERENCE_FEATURES.get(key)
        if attr is None:
            logger.debug("preference: ignoring unknown preference key %r", key)
            continue
        value = getattr(features, attr, None)
        if value is None:
            continue
        weighted += weight * value
        total += weight
    return _clamp01(weighted / total) if total > 0 else 0.0


def experience_score(
    activities: Sequence[Activity],
    nights: int,
    *,
    activities_per_day: int = DEFAULT_ACTIVITIES_PER_DAY,
) -> float:
    """Coverage: how well the available activities fill the stay.

    ``min(n_activities / target, 1.0)`` where ``target = max(1, nights *
    activities_per_day)`` — the activities are already preference-filtered by the
    ExperienceAgent, so their count is the fit signal. No activities -> ``0.0``; a
    full slate -> ``1.0``. Deterministic.
    """
    target = max(1, nights * max(1, activities_per_day))
    return _clamp01(len(activities) / target)


def weather_score(weather: WeatherProfile | None) -> float:
    """Fraction of forecast days that are *not* a weather conflict.

    ``1 - conflict_days / total_days``. A candidate with no forecast
    (weather-unchecked) scores a neutral :data:`WEATHER_UNCHECKED_SCORE` — the
    unknown is neither rewarded nor penalized. Deterministic.
    """
    if weather is None or not weather.days:
        return WEATHER_UNCHECKED_SCORE
    conflicts = sum(1 for day in weather.days if day.weather_conflict)
    return _clamp01(1.0 - conflicts / len(weather.days))


def accommodation_score(accommodations: Sequence[Accommodation]) -> float:
    """Quality of the lodging on offer, from the best available rating.

    ``best_rating / 5``. With options but no ratings -> neutral
    :data:`UNRATED_ACCOMMODATION_SCORE`; with no options at all -> ``0.0`` (you
    cannot stay). Price is deliberately left to the cost factor, not double-counted
    here. Deterministic.
    """
    if not accommodations:
        return 0.0
    ratings = [a.rating for a in accommodations if a.rating is not None]
    if not ratings:
        return UNRATED_ACCOMMODATION_SCORE
    return _clamp01(max(ratings) / _MAX_RATING)


# --- magnitude factors (normalized across the candidate set) ----------------


@dataclass(frozen=True)
class CostBreakdown:
    """The components of a candidate's total EUR trip cost (for the response/comparison).

    ``transport`` is the reach cost; ``accommodation`` is the **median** nightly price of
    the representative options times the nights (a typical stay); ``activities`` is the
    sum of the known activity entrance fees. ``total`` mirrors :func:`trip_cost` — it is
    ``None`` exactly when transport cost is unknown (the trip cannot be costed), and
    otherwise equals ``transport + accommodation + activities``. A component is ``None``
    when its data is absent (no accommodation options, no transport).
    """

    transport: float | None
    accommodation: float | None
    activities: float
    total: float | None


def cost_breakdown(candidate: CandidateInputs) -> CostBreakdown:
    """Break a candidate's trip cost into transport / accommodation / activities.

    The single source of truth behind :func:`trip_cost` and the response's
    ``cost_breakdown`` / ranked-candidate cost fields. Deterministic.
    """
    transport = candidate.transport.cost if candidate.transport is not None else None
    prices = [a.price_per_night for a in candidate.accommodations if a.price_per_night is not None]
    accommodation = statistics.median(prices) * max(1, candidate.nights) if prices else None
    activities = sum(a.cost for a in candidate.activities if a.cost is not None)
    # Transport cost is required to cost the trip at all (it is the dominant,
    # distance-driven component); without it the total is unknown, not a cheap partial.
    if transport is None:
        total = None
    else:
        total = transport + (accommodation or 0.0) + activities
    return CostBreakdown(
        transport=transport, accommodation=accommodation, activities=activities, total=total
    )


def trip_cost(candidate: CandidateInputs) -> float | None:
    """Total EUR trip cost: transport + typical accommodation * nights + activities.

    The **transport cost is required**: it is the dominant, distance-driven component
    that differentiates candidates, so a trip whose transport cost is unknown has an
    unknown total and returns ``None`` — the cost factor then penalizes it as missing
    rather than making it look artificially cheap for lacking a component. When
    transport cost is known, accommodation (the **median** nightly price of the
    representative options — a typical stay) and known activity entrance costs
    (``None`` = free/unknown) are added. Deterministic.
    """
    return cost_breakdown(candidate).total


def travel_minutes(candidate: CandidateInputs) -> float | None:
    """The journey duration in minutes (``None`` when transport/duration is unknown)."""
    transport = candidate.transport
    if transport is None or transport.duration_minutes is None:
        return None
    return float(transport.duration_minutes)


# ============================================================================
# Per-activity valuation + representative accommodation (Phase-6 reconciliation)
# ============================================================================
#
# The optimizer schedules only a subset of a destination's activities. To keep the
# *most preference-relevant* ones (rather than an arbitrary subset that merely fits the
# time budget), each activity is valued against the user's preference vector and that
# value drives the scheduler's selection. The mapping below is deterministic
# classification logic (an activity's OSM/OpenTripMap category → a preference theme),
# not ingested data — analogous to the labelled approximation constants elsewhere.

# Baseline value every schedulable activity carries, so an off-theme or uncategorized
# activity is still worth doing (and fills spare time) rather than being ignored.
_ACTIVITY_VALUE_BASE = 0.3
# Value used when preferences or the activity's theme give no signal either way.
_NEUTRAL_ACTIVITY_VALUE = 0.5

# Preference theme -> substrings that identify it in an activity's free-text category.
# Matched case-insensitively as substrings, covering the common POI category words.
_ACTIVITY_THEME_KEYWORDS: Mapping[str, tuple[str, ...]] = {
    "culture": (
        "museum",
        "gallery",
        "monument",
        "castle",
        "historic",
        "architecture",
        "church",
        "cathedral",
        "palace",
        "memorial",
        "theatre",
        "theater",
        "cultural",
        "heritage",
        "ruins",
        "archaeolog",
        "fort",
        "tower",
    ),
    "nature": (
        "park",
        "garden",
        "nature",
        "beach",
        "viewpoint",
        "mountain",
        "lake",
        "forest",
        "natural",
        "waterfall",
        "hill",
        "reserve",
        "zoo",
        "botanic",
    ),
    "nightlife": ("bar", "club", "nightlife", "pub", "nightclub", "lounge"),
    "food": ("restaurant", "cafe", "food", "market", "cuisine", "winery", "brewery"),
    "tourism": ("attraction", "tourism", "landmark", "sightseeing", "tourist", "square"),
}


def _activity_theme(category: str | None) -> str | None:
    """Classify an activity's category into a preference theme (``None`` if unknown)."""
    if not category:
        return None
    lowered = category.lower()
    for theme, keywords in _ACTIVITY_THEME_KEYWORDS.items():
        if any(word in lowered for word in keywords):
            return theme
    return None


def activity_value(activity: Activity, preferences: PreferenceWeights) -> float:
    """How well one activity fits the user's preferences, in ``[0, 1]``.

    The activity's category is classified into a preference theme; the value is the
    theme's share of the user's positive preference weight, lifted onto a
    ``[_ACTIVITY_VALUE_BASE, 1]`` floor so an off-theme activity still has worth (it can
    fill spare time) but a preferred-theme activity scores higher. With no usable
    preferences or an unrecognised category it returns a neutral value. Deterministic.
    """
    positive = {k: w for k, w in preferences.items() if w and w > 0 and k in _PREFERENCE_FEATURES}
    theme = _activity_theme(activity.category)
    if not positive or theme is None:
        return _NEUTRAL_ACTIVITY_VALUE
    share = positive.get(theme, 0.0) / sum(positive.values())
    return _clamp01(_ACTIVITY_VALUE_BASE + (1.0 - _ACTIVITY_VALUE_BASE) * share)


def activity_values(activities: Sequence[Activity], preferences: PreferenceWeights) -> list[float]:
    """:func:`activity_value` for each activity, aligned to input order."""
    return [activity_value(a, preferences) for a in activities]


def representative_accommodation(
    accommodations: Sequence[Accommodation],
) -> Accommodation | None:
    """Pick the single representative lodging for the plan — a *typical* priced stay.

    Chooses the median-priced option (ties broken toward the better rating), matching
    the median nightly price :func:`cost_breakdown` uses, so the reported plan's
    accommodation and its cost are consistent. Returns ``None`` for no options, and the
    first option when none carry a price. Deterministic.
    """
    if not accommodations:
        return None
    priced = [a for a in accommodations if a.price_per_night is not None]
    if not priced:
        return accommodations[0]
    ordered = sorted(priced, key=lambda a: (a.price_per_night, -(a.rating or 0.0)))
    return ordered[len(ordered) // 2]


# --- assembly ---------------------------------------------------------------


def compute_factor_scores(
    candidates: Sequence[CandidateInputs],
    *,
    weights: PreferenceWeights | None = None,
    activities_per_day: int = DEFAULT_ACTIVITIES_PER_DAY,
) -> list[FactorScores]:
    """Compute all six factor scores for each candidate (technical-doc §14).

    The intrinsic factors (preference, experience, weather, accommodation) are
    computed per candidate; the magnitude factors (cost, travel) are min-max
    normalized across the whole set and inverted, so cheaper/shorter scores higher.
    Order is preserved. Pure and deterministic for the same inputs.
    """
    if not candidates:
        return []
    weights = weights or {}
    cost_norm = minmax_normalize([trip_cost(c) for c in candidates], invert=LOWER_IS_BETTER)
    travel_norm = minmax_normalize([travel_minutes(c) for c in candidates], invert=LOWER_IS_BETTER)
    scores = [
        FactorScores(
            cost=cost_norm[i],
            preference=preference_score(c.features, weights),
            experience=experience_score(
                c.activities, c.nights, activities_per_day=activities_per_day
            ),
            weather=weather_score(c.weather),
            travel=travel_norm[i],
            accommodation=accommodation_score(c.accommodations),
        )
        for i, c in enumerate(candidates)
    ]
    logger.debug("scorer: computed factor scores for %d candidate(s)", len(scores))
    return scores


# ============================================================================
# Weighted overall score (BS-070) — configurable, preference-modulated weights
# ============================================================================
#
# The six factor scores collapse into one ``overall_score`` per candidate — a
# weighted sum minus any soft penalties (technical-doc §14, ADR-006):
#
#     overall_score = Σ (weight_f · factor_f) − penalty
#
# Weights are configurable (:class:`FactorWeights`; the defaults below are the
# "config") and are **modulated by the user's preferences**: a thematically
# concentrated preference (culture-heavy) lifts the ``preference`` factor's weight,
# so the ranking tilts toward destinations that fit what the user cares about. The
# weights are normalized at use, so lifting one factor proportionally lowers the
# rest. Scoring stays separate from the optimizer's hard constraints/objective
# (ADR-006): ``penalty`` here is a soft deduction the caller supplies, not a
# feasibility rule.

# Baseline factor weights (the request-level "config"): a budget-first profile.
# Need not sum to 1 — they are normalized when applied.
DEFAULT_WEIGHTS_MAP = {
    "cost": 0.30,
    "preference": 0.25,
    "experience": 0.15,
    "weather": 0.10,
    "travel": 0.10,
    "accommodation": 0.10,
}

# How strongly a concentrated preference lifts the preference factor weight:
# modulated = base · (1 + strength · peak_share). At 1.0, an all-in thematic
# preference (one theme with all the weight) doubles the preference weight.
DEFAULT_PREFERENCE_MODULATION_STRENGTH = 1.0

_FACTOR_NAMES = ("cost", "preference", "experience", "weather", "travel", "accommodation")


@dataclass(frozen=True)
class FactorWeights:
    """Relative importance of each factor in the overall score (the scoring config).

    Any non-negative weights are accepted; only their ratios matter because
    :meth:`normalized` rescales them to sum to 1 before use. At least one must be
    positive.
    """

    cost: float = DEFAULT_WEIGHTS_MAP["cost"]
    preference: float = DEFAULT_WEIGHTS_MAP["preference"]
    experience: float = DEFAULT_WEIGHTS_MAP["experience"]
    weather: float = DEFAULT_WEIGHTS_MAP["weather"]
    travel: float = DEFAULT_WEIGHTS_MAP["travel"]
    accommodation: float = DEFAULT_WEIGHTS_MAP["accommodation"]

    def __post_init__(self) -> None:
        for name in _FACTOR_NAMES:
            value = getattr(self, name)
            if value < 0:
                raise ValueError(f"weight {name} must be non-negative, got {value!r}")
        if self.total() <= 0:
            raise ValueError("at least one factor weight must be positive")

    def total(self) -> float:
        return sum(getattr(self, name) for name in _FACTOR_NAMES)

    def normalized(self) -> FactorWeights:
        """A copy whose weights sum to 1.0 (preserving their ratios)."""
        total = self.total()
        return FactorWeights(**{name: getattr(self, name) / total for name in _FACTOR_NAMES})


DEFAULT_WEIGHTS = FactorWeights()


def modulate_weights(
    base: FactorWeights,
    preferences: PreferenceWeights | None,
    *,
    strength: float = DEFAULT_PREFERENCE_MODULATION_STRENGTH,
) -> FactorWeights:
    """Lift the ``preference`` factor weight by how concentrated the user's prefs are.

    ``peak_share`` is the largest single preference's share of the total positive
    preference weight (1.0 = all-in on one theme, e.g. culture-heavy); the preference
    factor weight becomes ``base.preference · (1 + strength · peak_share)``. With no
    positive preferences the weights are returned unchanged. The other weights are
    left as-is and shrink proportionally only when :meth:`FactorWeights.normalized`
    is applied. Deterministic.
    """
    positive = {k: w for k, w in (preferences or {}).items() if w and w > 0}
    if not positive:
        return base
    peak_share = max(positive.values()) / sum(positive.values())
    boosted = base.preference * (1.0 + max(0.0, strength) * peak_share)
    logger.debug(
        "scorer: modulating preference weight %.3f -> %.3f (peak_share=%.2f)",
        base.preference,
        boosted,
        peak_share,
    )
    return replace(base, preference=boosted)


def overall_score(
    factors: FactorScores,
    weights: FactorWeights = DEFAULT_WEIGHTS,
    *,
    penalty: float = 0.0,
) -> float:
    """Weighted sum of the factor scores minus ``penalty``, clamped to ``[0, 1]``.

    Weights are normalized to sum to 1, so the weighted sum is in ``[0, 1]``; a
    non-negative ``penalty`` (a soft deduction from the caller — never a hard
    constraint) lowers it. A negative penalty is rejected — it would be a silent
    bonus, not a deduction. Deterministic.
    """
    if penalty < 0:
        raise ValueError(f"penalty must be non-negative, got {penalty!r}")
    w = weights.normalized()
    # Sum over the canonical factor names so a new factor cannot be silently omitted.
    score = sum(getattr(w, name) * getattr(factors, name) for name in _FACTOR_NAMES)
    return _clamp01(score - penalty)


def score_candidates(
    factor_scores: Sequence[FactorScores],
    *,
    weights: FactorWeights = DEFAULT_WEIGHTS,
    preferences: PreferenceWeights | None = None,
    penalties: Sequence[float] | None = None,
) -> list[float]:
    """Overall score for each candidate, with preference-modulated weights applied once.

    ``penalties`` (optional) must align with ``factor_scores`` one-to-one; omitted
    means no penalties. The weights are modulated by ``preferences`` a single time and
    reused for every candidate, so the ranking is consistent and deterministic.
    """
    if penalties is not None and len(penalties) != len(factor_scores):
        raise ValueError(
            f"penalties length ({len(penalties)}) must match factor_scores "
            f"({len(factor_scores)})"
        )
    effective = modulate_weights(weights, preferences)
    pens = penalties if penalties is not None else [0.0] * len(factor_scores)
    return [
        overall_score(f, effective, penalty=p) for f, p in zip(factor_scores, pens, strict=True)
    ]
