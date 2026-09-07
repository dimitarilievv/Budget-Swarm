"""Indoor / outdoor / mixed classification of activities — curated mapping (BS-066).

This is the **feature-engineering step** that gives every :class:`~ai_engine.domain.models.Activity`
an ``indoor_outdoor`` class, and it is the **interface to weather replanning**
(technical-doc §11.3 / §13): the optimizer downgrades weather-exposed (``outdoor``)
activities on wet or cold days, keeps ``indoor`` ones, and treats ``mixed`` as
partially exposed. Because that decision hinges entirely on this class, the mapping
is kept here as a small, curated, auditable table rather than being inferred ad hoc.

**The mapping (whole-token match against an activity's normalized category tokens —
OpenTripMap ``kinds`` and OpenStreetMap tag values, split on separators and
singularized so "park" matches "gardens_and_parks" but never "parking"):**

* ``indoor`` — museums, galleries, theatres, cinemas, malls, aquariums, libraries,
  casinos, spas, restaurants/bars/clubs, shops: venues you visit under a roof
  (:data:`INDOOR_KEYWORDS`).
* ``outdoor`` — nature, parks and gardens, beaches, viewpoints, islands, geological
  formations, historic sites/monuments/castles/ruins, and waterfalls/mountains/
  trails/zoos whose visit is out in the open (:data:`OUTDOOR_KEYWORDS`).
* ``mixed`` — the deliberate default when a token matches **nothing** (ambiguous,
  e.g. a bare "attraction" or "interesting_places") **or** matches indoor and outdoor
  keywords equally (a genuine tie). When both classes match but one has *more*
  distinct matching tokens, that dominant class wins rather than collapsing to
  ``mixed``; only a true tie is ``mixed``, so weather replanning stays conservative.

:func:`classify_activity` is what agents call: it applies the table to the tokens
and, only when the table is ambiguous, falls back to a definite provider-supplied
hint (the source-level classification the AttractionProvider derived from raw OSM
tags, BS-051) before finally defaulting to ``mixed``. So every activity always gets
a class, the curated table wins when it has a definite answer, and nothing is left
unclassified.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Iterable

from ai_engine.domain import IndoorOutdoor

logger = logging.getLogger(__name__)

# Curated keyword → class tables (technical-doc §11.3), as canonical single-word
# tokens. A category value is normalized into whole, singularized tokens (splitting
# on ``_``, ``-``, spaces, commas) plus a separator-stripped joined form, and a
# keyword matches only as a **whole token** — so "park" matches the kind
# "gardens_and_parks" but never an unrelated "parking". Kept documented; extend
# deliberately.
INDOOR_KEYWORDS: frozenset[str] = frozenset(
    {
        "museum",
        "gallery",
        "theatre",
        "theater",
        "cinema",
        "mall",
        "aquarium",
        "library",
        "casino",
        "thermal",
        "spa",
        "restaurant",
        "cafe",
        "bar",
        "pub",
        "nightclub",
        "club",
        "shop",
        "shopping",
        "boutique",
        "concert",
        "opera",
        "planetarium",
        "bowling",
        "indoor",
    }
)

OUTDOOR_KEYWORDS: frozenset[str] = frozenset(
    {
        "natural",
        "nature",
        "garden",
        "park",
        "beach",
        "viewpoint",
        "geological",
        "island",
        "historic",
        "monument",
        "castle",
        "ruin",
        "archaeology",
        "waterfall",
        "mountain",
        "hiking",
        "trail",
        "square",
        "lake",
        "river",
        "forest",
        "cliff",
        "canyon",
        "glacier",
        "volcano",
        "vineyard",
        "harbour",
        "pier",
        "promenade",
        "botanical",
        "zoo",
        "outdoor",
    }
)

_TOKEN_SPLIT = re.compile(r"[^a-z0-9]+")


def _singular(token: str) -> str:
    """Crudely singularize a token so plurals collapse ('galleries'->'gallery',
    'beaches'->'beach', 'parks'->'park')."""
    if len(token) > 4 and token.endswith("ies"):
        return token[:-3] + "y"
    if len(token) > 4 and token.endswith(("ches", "shes", "sses", "xes", "zes")):
        return token[:-2]
    if len(token) > 3 and token.endswith("s") and not token.endswith("ss"):
        return token[:-1]
    return token


def _tokenize(values: Iterable[str]) -> set[str]:
    """Normalize category strings into whole, singularized tokens for matching.

    Each value is lower-cased and split on non-alphanumerics; every part is
    singularized, and the separator-stripped joined form is added too (so
    "view_points" yields both 'view'/'point' and 'viewpoint').
    """
    tokens: set[str] = set()
    for value in values:
        if not value:
            continue
        parts = [p for p in _TOKEN_SPLIT.split(value.lower()) if p]
        tokens.update(_singular(p) for p in parts)
        joined = "".join(parts)
        if joined:
            tokens.add(_singular(joined))
    return tokens


def coerce(value: object) -> IndoorOutdoor:
    """Map a value (enum, ``'indoor'``/``'outdoor'``/``'mixed'`` string, or junk) onto
    the enum, defaulting to :attr:`IndoorOutdoor.MIXED` for anything unrecognised.

    This is also the validation gate for a provider hint: an unknown string can
    never be trusted as a classification, it degrades to ``mixed``."""
    if isinstance(value, IndoorOutdoor):
        return value
    try:
        return IndoorOutdoor(value)
    except (ValueError, TypeError):
        return IndoorOutdoor.MIXED


def classify_categories(tokens: Iterable[str]) -> IndoorOutdoor:
    """Classify from category tokens alone via the curated tables.

    Matching is on whole, normalized tokens. ``indoor`` when only indoor keywords
    match and ``outdoor`` when only outdoor ones do; when **both** match, the class
    with more distinct matching tokens wins (a museum with a garden tilts to whichever
    dominates), and a genuine tie — or no match at all — is :attr:`IndoorOutdoor.MIXED`.
    """
    present = _tokenize(tokens)
    indoor = len(present & INDOOR_KEYWORDS)
    outdoor = len(present & OUTDOOR_KEYWORDS)
    if indoor > outdoor:
        return IndoorOutdoor.INDOOR
    if outdoor > indoor:
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
