"""Estimated visit-duration heuristic for activities (BS-065).

The domain :class:`~ai_engine.domain.models.Activity` carries an
``est_duration_minutes`` the optimizer uses to fit activities into a day
(technical-doc §11). No free POI source (OpenTripMap / Overpass) reports how long a
visit takes, so this is a **deterministic heuristic**, not ingested data — a small,
documented, configurable table keyed on the activity's category tokens, with a
single default when nothing matches. The value is an estimate by construction (the
field is ``est_``); it is never presented as a measured figure.

The table is intentionally coarse — a museum warrants longer than a viewpoint — and
matched as case-insensitive substrings against an activity's category tokens
(OpenTripMap ``kinds`` and OSM tag values), first match winning. Adjust the numbers
here in one place rather than scattering magic numbers through the agent.
"""

from __future__ import annotations

from collections.abc import Iterable

# Category keyword → typical visit minutes (heuristic, first substring match wins).
DURATION_MINUTES: tuple[tuple[str, int], ...] = (
    ("museum", 120),
    ("galler", 90),
    ("theatre", 120),
    ("theater", 120),
    ("cinema", 120),
    ("aquarium", 90),
    ("zoo", 150),
    ("mall", 90),
    ("beach", 120),
    ("national_park", 150),
    ("nature_reserve", 120),
    ("mountain", 120),
    ("hiking", 120),
    ("trail", 90),
    ("waterfall", 45),
    ("garden", 60),
    ("park", 90),
    ("natural", 90),
    ("castle", 75),
    ("archaeolog", 75),
    ("ruins", 60),
    ("historic", 60),
    ("monument", 45),
    ("view_point", 30),
    ("viewpoint", 30),
    ("square", 30),
)

# Fallback when no category keyword matches (an unclassified/ambiguous POI).
DEFAULT_DURATION_MINUTES = 60


def estimate_duration_minutes(tokens: Iterable[str]) -> int:
    """Heuristic estimated visit duration in minutes for the given category tokens.

    Returns the minutes for the first table keyword that appears (case-insensitive
    substring) in any token, else :data:`DEFAULT_DURATION_MINUTES`.
    """
    lowered = [t.lower() for t in tokens if t]
    for keyword, minutes in DURATION_MINUTES:
        if any(keyword in token for token in lowered):
            return minutes
    return DEFAULT_DURATION_MINUTES
