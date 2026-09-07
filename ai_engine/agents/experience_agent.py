"""ExperienceAgent — candidate POI retrieval per destination (BS-064).

ExperienceAgent answers "what is there to do here?" for a candidate city
(technical-doc §11). It retrieves points of interest through the
:class:`~ai_engine.providers.attraction.AttractionService` (BS-051) — OpenTripMap
primary, Overpass fallback, both ODbL/OpenStreetMap-derived and cached per
destination (§23) — filters them to the user's **preference categories**, bounds
the result, and maps each POI onto the normalized domain
:class:`~ai_engine.domain.models.Activity`.

Two deliberate shaping decisions:

* **Category filtering.** A traveller who wants museums and nightlife should not
  be handed every park in the city. The agent keeps a POI when any of its category
  tokens matches a requested preference keyword (case-insensitive substring), so
  the coarse OpenTripMap ``kinds`` / OSM tags map onto high-level preferences
  without a brittle fixed lookup. With no preferences given, nothing is filtered.
* **A bounded, popularity-first set.** The provider is asked for a stable
  candidate pool per destination (so its per-destination cache stays effective),
  the pool is filtered, and only the top ``limit`` are returned — OpenTripMap
  already orders by popularity, so the most notable matching POIs survive.

Every activity is assigned an ``indoor_outdoor`` class during normalization via the
curated mapping in :mod:`ai_engine.agents.indoor_outdoor` (BS-066) — the interface
to weather replanning (technical-doc §11.3) — with the provider's source-level
classification used only as a fallback when the mapping is ambiguous.

Normalization is the single exit point: only the domain fields the coordinator
needs are carried across; the provider label, popularity score, source id and
Wikidata id do **not** leak into the domain shape (technical-doc §17/§19). POIs are
never approximated — Overpass is an always-available real source — so an option is
flagged ``real`` (or ``cached`` when served from the cache), never ``estimated``.
"Degrade, don't crash" (ADR-005): a destination with no POIs at all yields an empty
list, logged, rather than an error.

``ai_engine`` stays free of any backend/ORM import: the attraction source and its
cache are injected into the provider (token/cache wiring is the backend/pipeline's
job). This module depends only on the domain models, the attraction provider and
the standard library.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence

from ai_engine.agents.indoor_outdoor import classify_activity
from ai_engine.domain import Activity, QualityFlag
from ai_engine.providers.attraction import Activity as AttractionActivity
from ai_engine.providers.attraction import AttractionRequest, AttractionService
from ai_engine.utils.api_clients import ProviderError, ProviderResult

logger = logging.getLogger(__name__)

# Final bounded output size, its hard cap, and the per-destination candidate pool
# requested from the provider. The pool is a stable constant so the provider's
# per-destination cache key does not change with the caller's ``limit`` — keeping
# retrieval cache-effective (technical-doc §23).
DEFAULT_LIMIT = 10
MAX_LIMIT = 20
CANDIDATE_POOL = 50


class ExperienceAgent:
    """Retrieve preference-filtered POIs for a destination (technical-doc §11).

    Constructed with an :class:`AttractionService` (OpenTripMap → Overpass).
    :meth:`activities_for` returns a bounded list of normalized :class:`Activity`
    domain models for one destination centre, filtered to the requested categories.
    """

    def __init__(
        self,
        attractions: AttractionService,
        *,
        default_limit: int = DEFAULT_LIMIT,
        max_limit: int = MAX_LIMIT,
        candidate_pool: int = CANDIDATE_POOL,
    ) -> None:
        if not 1 <= default_limit <= max_limit:
            raise ValueError(
                f"default_limit must be within [1, max_limit={max_limit}], got {default_limit}"
            )
        if candidate_pool < max_limit:
            raise ValueError(
                f"candidate_pool ({candidate_pool}) must be >= max_limit ({max_limit})"
            )
        self.attractions = attractions
        self.default_limit = default_limit
        self.max_limit = max_limit
        self.candidate_pool = candidate_pool

    def activities_for(
        self,
        center,
        *,
        destination: str | None = None,
        categories: Sequence[str] | None = None,
        radius_m: int = 5000,
        limit: int | None = None,
    ) -> list[Activity]:
        """Return up to ``limit`` preference-matching :class:`Activity`s for a city.

        Fetches a stable candidate pool from the :class:`AttractionService` (cached
        per destination), filters it to ``categories`` (keyword match; ``None`` keeps
        all), and returns the top ``bound`` normalized domain activities. A
        destination with no POIs yields an empty list (logged), never an error.
        """
        bound = self._bounded_limit(limit)
        request = AttractionRequest(
            center=center,
            destination=destination,
            radius_m=radius_m,
            limit=self.candidate_pool,
        )

        try:
            result: ProviderResult[list[AttractionActivity]] = self.attractions.resolve(request)
        except ProviderError as exc:
            logger.warning(
                "experience: no POIs for destination %s (%s)", destination or center, exc
            )
            return []

        quality = QualityFlag(result.quality_flag)
        wanted = _normalize_keywords(categories)
        matched = [poi for poi in (result.data or []) if _matches(poi, wanted)]
        activities = [self._normalize(poi, quality) for poi in matched[:bound]]
        logger.debug(
            "experience: destination %s -> %d/%d POIs after category filter %s [%s]",
            destination or center,
            len(activities),
            len(result.data or []),
            sorted(wanted) or "none",
            quality,
        )
        return activities

    def _bounded_limit(self, limit: int | None) -> int:
        """Clamp a requested count into ``[1, max_limit]`` (default when None)."""
        requested = self.default_limit if limit is None else limit
        return max(1, min(self.max_limit, requested))

    def _normalize(self, poi: AttractionActivity, quality: QualityFlag) -> Activity:
        """Map a provider POI onto the domain :class:`Activity` (single exit point).

        Only the normalized fields survive; the provider label, popularity, source id
        and Wikidata id do not leak into the domain shape (technical-doc §19). POI
        entrance cost / duration are unknown at retrieval and left ``None``.
        """
        return Activity(
            name=poi.name,
            category=poi.category,
            latitude=poi.latitude,
            longitude=poi.longitude,
            # Curated indoor/outdoor feature step (BS-066): classify from the POI's
            # category tokens, falling back to the provider's source-level hint.
            indoor_outdoor=classify_activity(
                [poi.category, *poi.categories], hint=poi.indoor_outdoor
            ),
            quality_flag=quality,
        )


def _normalize_keywords(categories: Sequence[str] | None) -> set[str]:
    """Lower-cased, non-empty preference keywords (empty set = no filtering)."""
    if not categories:
        return set()
    return {c.strip().lower() for c in categories if c and c.strip()}


def _matches(poi: AttractionActivity, keywords: set[str]) -> bool:
    """True when a POI has no filter to satisfy, or any category token matches.

    A keyword matches if it is a case-insensitive substring of any of the POI's
    category tokens (its primary ``category`` plus the full ``categories`` list), so
    "museum" matches the OpenTripMap kind "museums".
    """
    if not keywords:
        return True
    tokens = [t.lower() for t in ([poi.category, *poi.categories]) if t]
    return any(keyword in token for keyword in keywords for token in tokens)
