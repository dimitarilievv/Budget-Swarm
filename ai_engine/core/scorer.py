"""Per-candidate-set normalization utilities for scoring (BS-068).

The scorer turns each candidate destination's raw factors — cost, travel time,
culture/nature/food scores, weather, … — into a single comparable number
(technical-doc §14). Those raw factors live on wildly different scales (euros vs.
minutes vs. a 0–1 index), so before they can be weighted and summed they must be
brought onto one common ``[0, 1]`` scale. This module is that first step: **min-max
normalization computed per request's candidate set** (BS-069 does the weighting on
top).

Two things make this more than a textbook min-max:

* **Direction.** For a "higher is better" factor (a culture score) the largest raw
  value should map to ``1.0``; for a "lower is better" factor (cost, travel time)
  the *smallest* raw value should map to ``1.0``. Pass ``invert=True`` (or
  :data:`LOWER_IS_BETTER`) for the latter, so every normalized factor reads the same
  way downstream: **bigger is always better**.
* **Degenerate sets.** A single candidate, or a set whose values are all equal, has
  a zero range — the ``(x - min) / (max - min)`` formula divides by zero. That is not
  an error here (a one-city request is normal): every such candidate is given the
  same neutral :data:`DEGENERATE_VALUE`, so the factor simply stops differentiating
  candidates instead of crashing.

Missing data is handled explicitly: a ``None`` value (an un-enriched candidate) is
excluded from the min/max range and assigned :data:`MISSING_VALUE` — ``0.0`` by
default, so unknown data is penalized rather than silently rewarded.

This module is deterministic and dependency-free — no I/O, no randomness, no domain
import — so it is trivially unit-testable and reusable by the coordinator/optimizer.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping, Sequence

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
