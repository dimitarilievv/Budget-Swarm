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
  be handed every park in the city. The agent keeps a POI when one of its
  normalized category tokens matches a requested preference — matching is on whole,
  singularized tokens expanded through a small alias map (so "nature" pulls in
  "park"/"garden"/… and "park" no longer matches an unrelated "parking"), mapping
  the coarse OpenTripMap ``kinds`` / OSM tags onto high-level preferences. With no
  preferences given, nothing is filtered.
* **A bounded, popularity-first set.** The provider is asked for a stable
  candidate pool per destination (so its per-destination cache stays effective),
  the pool is filtered, and only the top ``limit`` are returned — OpenTripMap
  already orders by popularity, so the most notable matching POIs survive.

Every activity is assigned an ``indoor_outdoor`` class during normalization via the
curated mapping in :mod:`ai_engine.agents.indoor_outdoor` (BS-066) — the interface
to weather replanning (technical-doc §11.3) — with the provider's source-level
classification used only as a fallback when the mapping is ambiguous. It also gets a
heuristic ``est_duration_minutes`` (:mod:`ai_engine.agents.duration`, BS-065) so the
optimizer can fit activities into a day. Both are MVP-required normalization steps.

**Wikipedia enrichment** (BS-065) is a Post-MVP, optional extra: when an
:class:`~ai_engine.providers.enrichment.ActivityEnricher` is configured and
``enrich=True`` is passed, each activity also gets a short ``description`` summary;
a lookup failure simply leaves it ``None``, and the plan is fully usable without it.

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
import re
from collections.abc import Iterable, Sequence

from ai_engine.agents.duration import estimate_duration_minutes
from ai_engine.agents.indoor_outdoor import classify_activity
from ai_engine.domain import Activity, QualityFlag
from ai_engine.providers.attraction import Activity as AttractionActivity
from ai_engine.providers.attraction import AttractionRequest, AttractionService
from ai_engine.providers.enrichment import ActivityEnricher
from ai_engine.utils.api_clients import (
    AllProvidersFailed,
    ProviderError,
    ProviderResult,
    ProviderUnavailable,
)

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
        enricher: ActivityEnricher | None = None,
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
        self.enricher = enricher
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
        enrich: bool = False,
    ) -> list[Activity]:
        """Return up to ``limit`` preference-matching :class:`Activity`s for a city.

        Fetches a stable candidate pool from the :class:`AttractionService` (cached
        per destination), filters it to ``categories`` (keyword match; ``None`` keeps
        all), and returns the top ``bound`` normalized domain activities, each with a
        heuristic ``est_duration_minutes`` and indoor/outdoor class. When ``enrich``
        is set and an enricher is configured, each activity also gets an optional
        summary (Post-MVP; a lookup failure simply leaves it ``None``). A destination
        with no POIs yields an empty list (logged), never an error.
        """
        if limit is not None and limit < 1:
            raise ValueError(f"limit must be >= 1, got {limit!r}")
        if radius_m < 1:
            raise ValueError(f"radius_m must be a positive number of metres, got {radius_m!r}")
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
            # Distinguish "this city genuinely has no POIs" from "the POI source
            # failed": both degrade to an empty list (the trip proceeds), but a real
            # source failure is logged louder so it is visible in operations.
            if _is_source_failure(exc):
                logger.warning(
                    "experience: POI source failed for %s (%s); proceeding with no activities",
                    destination or center,
                    exc,
                )
            else:
                logger.info(
                    "experience: no POIs for destination %s (%s)", destination or center, exc
                )
            return []

        quality = QualityFlag(result.quality_flag)
        wanted = _wanted_tokens(_normalize_keywords(categories))
        pool = _dedupe_pois(result.data or [])
        matched = [poi for poi in pool if _matches(poi, wanted)]
        activities = [self._normalize(poi, quality, enrich=enrich) for poi in matched[:bound]]
        logger.debug(
            "experience: destination %s -> %d/%d POIs after category filter %s [%s, enrich=%s]",
            destination or center,
            len(activities),
            len(result.data or []),
            sorted(wanted) or "none",
            quality,
            enrich,
        )
        return activities

    def _bounded_limit(self, limit: int | None) -> int:
        """Clamp a requested count into ``[1, max_limit]`` (default when None)."""
        requested = self.default_limit if limit is None else limit
        return max(1, min(self.max_limit, requested))

    def _normalize(
        self, poi: AttractionActivity, quality: QualityFlag, *, enrich: bool = False
    ) -> Activity:
        """Map a provider POI onto the domain :class:`Activity` (single exit point).

        Assigns the indoor/outdoor class (BS-066) and a heuristic
        ``est_duration_minutes`` (BS-065), and — when ``enrich`` is set and an
        enricher is configured — an optional summary. Only normalized fields survive;
        the provider label, popularity, source id and Wikidata id do not leak into the
        domain shape (technical-doc §19). POI entrance cost is unknown and left
        ``None``.
        """
        tokens = [poi.category, *poi.categories]
        return Activity(
            name=poi.name,
            category=poi.category,
            latitude=poi.latitude,
            longitude=poi.longitude,
            indoor_outdoor=classify_activity(tokens, hint=poi.indoor_outdoor),
            est_duration_minutes=estimate_duration_minutes(tokens),
            opening_hours=getattr(poi, "opening_hours", None),
            description=self._enrich(poi) if enrich else None,
            quality_flag=quality,
        )

    def _enrich(self, poi: AttractionActivity) -> str | None:
        """Optional Wikipedia summary for a POI, or ``None`` (never raises).

        Post-MVP: only runs when an enricher is configured; a lookup failure or a
        missing page degrades to ``None`` so enrichment never blocks the plan. The
        call is guarded here too, so even a misbehaving enricher that raises cannot
        interrupt the main planning flow (ADR-005).
        """
        if self.enricher is None:
            return None
        try:
            return self.enricher.summary_for(poi.name, poi.wikidata)
        except Exception as exc:  # noqa: BLE001 - enrichment is optional; never fatal
            logger.info("experience: enrichment failed for %r (%s); no description", poi.name, exc)
            return None


# High-level preference keyword -> the canonical category tokens that satisfy it.
# Lets a user ask for "nature" or "culture" and match the fine-grained OpenTripMap
# kinds / OSM tags, while keeping matching to whole tokens (so "park" no longer
# matches "parking" the way a bare substring test did). Extend deliberately.
PREFERENCE_ALIASES: dict[str, frozenset[str]] = {
    "museum": frozenset({"museum", "gallery", "exhibition"}),
    "culture": frozenset(
        {
            "museum",
            "gallery",
            "theatre",
            "theater",
            "monument",
            "historic",
            "castle",
            "heritage",
            "architecture",
            "cultural",
            "cathedral",
            "church",
            "palace",
        }
    ),
    "history": frozenset({"historic", "monument", "castle", "ruin", "archaeology", "heritage"}),
    "art": frozenset({"gallery", "museum", "art", "sculpture"}),
    "nature": frozenset(
        {
            "park",
            "garden",
            "natural",
            "beach",
            "forest",
            "mountain",
            "lake",
            "river",
            "viewpoint",
            "nature",
            "reserve",
            "waterfall",
            "island",
        }
    ),
    "nightlife": frozenset({"bar", "club", "nightclub", "pub", "nightlife", "casino"}),
    "food": frozenset({"restaurant", "cafe", "food", "market", "winery", "brewery", "bakery"}),
    "shopping": frozenset({"mall", "market", "shop", "shopping", "boutique"}),
    "religion": frozenset({"church", "cathedral", "mosque", "temple", "synagogue", "religion"}),
}

_TOKEN_SPLIT = re.compile(r"[^a-z0-9]+")


def _dedupe_pois(pois: Sequence[AttractionActivity]) -> list[AttractionActivity]:
    """Collapse POIs that refer to the same named place, keeping the first (best-ranked).

    A single venue is often returned several times: OpenStreetMap maps a theatre as both
    a node and a building way (their coordinates can differ by 100+ m), and OpenTripMap
    lists the same place under several ``kinds`` — each becoming a separate POI whose
    category label may differ (e.g. "architecture" vs "cultural"). Left unmerged, the
    scheduler would place the same place on two different days under two labels. Since a
    POI name from these sources is specific (not a bare "Park"), dedupe on the normalized
    **name** within the destination's search radius — robust to the coordinate drift that
    a location-based key misses. Input order is preserved (the pool is popularity-sorted
    upstream), so the retained entry is the best-ranked occurrence; unnamed POIs, which
    cannot be compared, are always kept.
    """
    seen: set[str] = set()
    out: list[AttractionActivity] = []
    for poi in pois:
        name = (poi.name or "").strip().lower()
        if not name:
            out.append(poi)  # unnamed POIs are not comparable; keep as-is
            continue
        if name in seen:
            continue
        seen.add(name)
        out.append(poi)
    return out


def _is_source_failure(exc: ProviderError) -> bool:
    """True when a resolve failure was a source being unreachable, not just empty.

    ``AllProvidersFailed`` records each attempt's reason; if any reason is a
    :class:`ProviderUnavailable` the source genuinely failed (vs. every provider
    simply having no data for this city). A bare :class:`ProviderUnavailable` counts
    too; a :class:`ProviderNoResult` is "no data", not a failure.
    """
    if isinstance(exc, AllProvidersFailed):
        return any(isinstance(reason, ProviderUnavailable) for _name, reason in exc.attempts)
    return isinstance(exc, ProviderUnavailable)


def _singular(token: str) -> str:
    """Crudely singularize a token so 'parks' and 'park' compare equal."""
    return token[:-1] if len(token) > 3 and token.endswith("s") else token


def _tokenize(value: str | None) -> set[str]:
    """Split a category string into normalized, singularized word tokens."""
    if not value:
        return set()
    return {_singular(tok) for tok in _TOKEN_SPLIT.split(value.lower()) if tok}


def _normalize_keywords(categories: Sequence[str] | None) -> set[str]:
    """Lower-cased, non-empty preference keywords (empty set = no filtering)."""
    if not categories:
        return set()
    return {c.strip().lower() for c in categories if c and c.strip()}


def _wanted_tokens(keywords: Iterable[str]) -> set[str]:
    """Expand preference keywords into the set of canonical tokens that satisfy them.

    Each keyword contributes its own singularized token plus any alias tokens, so a
    high-level preference ("nature") pulls in its concrete categories ("park",
    "garden", …) while matching stays token-exact, not substring.
    """
    wanted: set[str] = set()
    for keyword in keywords:
        key = keyword.strip().lower()
        if not key:
            continue
        wanted.add(_singular(key))
        wanted.update(_singular(alias) for alias in PREFERENCE_ALIASES.get(key, ()))
    return wanted


def _matches(poi: AttractionActivity, wanted: set[str]) -> bool:
    """True when a POI has no filter to satisfy, or a category token matches one.

    Matching is on whole, singularized tokens (from the POI's ``category`` plus its
    full ``categories`` list) against the expanded preference tokens — so "museum"
    matches the kind "museums" and "nature" matches "gardens_and_parks", but "park"
    no longer matches an unrelated "parking".
    """
    if not wanted:
        return True
    tokens: set[str] = set()
    for value in (poi.category, *poi.categories):
        tokens |= _tokenize(value)
    return bool(tokens & wanted)
