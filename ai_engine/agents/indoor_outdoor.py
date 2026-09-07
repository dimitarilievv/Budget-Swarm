"""Indoor / outdoor / mixed classification of activities — curated mapping (BS-066).

This is the **feature-engineering step** that gives every :class:`~ai_engine.domain.models.Activity`
an ``indoor_outdoor`` class, and it is the **interface to weather replanning**
(technical-doc §11.3 / §13): the optimizer downgrades weather-exposed (``outdoor``)
activities on wet or cold days, keeps ``indoor`` ones, and treats ``mixed`` as
partially exposed. Because that decision hinges entirely on this class, the mapping
is kept here as a small, curated, auditable table rather than being inferred ad hoc.

**The mapping (case-insensitive substring match against an activity's category
tokens — OpenTripMap ``kinds`` and OpenStreetMap tag values):**

* ``indoor`` — museums, galleries, theatres, cinemas, malls, aquariums, libraries,
  casinos, thermal/indoor baths: venues you visit under a roof (:data:`INDOOR_KEYWORDS`).
* ``outdoor`` — nature, parks and gardens, beaches, viewpoints, islands, geological
  formations, national parks and nature reserves, and historic sites/monuments/
  castles/ruins/archaeological sites whose visit is out in the open
  (:data:`OUTDOOR_KEYWORDS`).
* ``mixed`` — the deliberate default when a token matches **nothing** (ambiguous,
  e.g. a bare "attraction" or "interesting_places") **or** matches *both* an indoor
  and an outdoor keyword (genuinely part-and-part, e.g. a castle with an indoor
  museum). Never guessed either way — ambiguity resolves to ``mixed`` so weather
  replanning stays conservative.

:func:`classify_activity` is what agents call: it applies the table to the tokens
and, only when the table is ambiguous, falls back to a definite provider-supplied
hint (the source-level classification the AttractionProvider derived from raw OSM
tags, BS-051) before finally defaulting to ``mixed``. So every activity always gets
a class, the curated table wins when it has a definite answer, and nothing is left
unclassified.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable

from ai_engine.domain import IndoorOutdoor

logger = logging.getLogger(__name__)

# Curated keyword → class tables (technical-doc §11.3). Matched as case-insensitive
# substrings so an OpenTripMap kind ("gardens_and_parks") or an OSM value ("museum")
# both resolve. Kept intentionally small and documented; extend deliberately.
INDOOR_KEYWORDS: tuple[str, ...] = (
    "museum",
    "galler",  # gallery / galleries
    "theatre",
    "theater",
    "cinema",
    "mall",
    "aquarium",
    "library",
    "casino",
    "thermal_bath",
    "indoor",
)

OUTDOOR_KEYWORDS: tuple[str, ...] = (
    "natural",
    "nature_reserve",
    "garden",
    "park",
    "beach",
    "view_point",
    "viewpoint",
    "geological",
    "island",
    "national_park",
    "historic",
    "monument",
    "castle",
    "ruins",
    "archaeolog",  # archaeology / archaeological
    "waterfall",
    "mountain",
    "hiking",
    "trail",
    "square",  # city squares / plazas
)


def coerce(value: object) -> IndoorOutdoor:
    """Map a value (enum, ``'indoor'``/``'outdoor'``/``'mixed'`` string, or junk) onto
    the enum, defaulting to :attr:`IndoorOutdoor.MIXED` for anything unrecognised."""
    if isinstance(value, IndoorOutdoor):
        return value
    try:
        return IndoorOutdoor(value)
    except (ValueError, TypeError):
        return IndoorOutdoor.MIXED


def classify_categories(tokens: Iterable[str]) -> IndoorOutdoor:
    """Classify from category tokens alone via the curated tables.

    ``indoor`` when only indoor keywords match, ``outdoor`` when only outdoor ones
    do, and :attr:`IndoorOutdoor.MIXED` when neither matches (ambiguous) or both do
    (genuinely part-and-part).
    """
    lowered = [t.lower() for t in tokens if t]
    indoor = any(keyword in token for keyword in INDOOR_KEYWORDS for token in lowered)
    outdoor = any(keyword in token for keyword in OUTDOOR_KEYWORDS for token in lowered)
    if indoor and not outdoor:
        return IndoorOutdoor.INDOOR
    if outdoor and not indoor:
        return IndoorOutdoor.OUTDOOR
    return IndoorOutdoor.MIXED


def classify_activity(tokens: Iterable[str], *, hint: object = None) -> IndoorOutdoor:
    """The classification agents call — table first, definite hint second, else mixed.

    Applies the curated tables to ``tokens``; if that is ambiguous (``mixed``) and a
    definite provider ``hint`` is available (``indoor`` / ``outdoor``), that hint is
    used, so the source-level OSM-tag classification (BS-051) is honoured where the
    keyword table cannot decide. Anything still ambiguous is ``mixed`` — every
    activity gets a class, and ambiguity never guesses.
    """
    primary = classify_categories(tokens)
    if primary is not IndoorOutdoor.MIXED:
        return primary
    hint_class = coerce(hint)
    if hint_class is not IndoorOutdoor.MIXED:
        return hint_class
    return IndoorOutdoor.MIXED
