"""StayAgent — representative accommodation selection per destination (BS-060).

StayAgent answers "where could I stay here, and roughly what does it cost?" for a
candidate city (technical-doc §10). It deliberately returns a **small, bounded set
of representative options** — budget / typical / comfort — rather than dumping every
listing, so the coordinator and optimizer reason over a handful of comparable
choices instead of hundreds of rows.

The selection itself is done by the :class:`~ai_engine.providers.accommodation.AccommodationService`
(BS-050): its primary reads the normalized ``accommodations`` table and samples the
price-sorted listings into representatives, and — for a city with no listings — its
fallback synthesizes options from that city's precomputed price statistics (Q1 /
median / Q3, BS-035), flagged ``estimated``. StayAgent orchestrates that call,
enforces the bound, and routes every option through a **single normalization exit
point** (:meth:`StayAgent._normalize`, BS-062) that maps it onto the normalized
domain :class:`~ai_engine.domain.models.Accommodation` — so the coordinator only ever
sees the provider-agnostic domain shape (technical-doc §17/§19), never a listing id,
vendor field or raw row.

Estimates are flagged honestly per option: the domain ``quality_flag`` is the
least-firm of the option's own provenance (a ``real`` listing vs. a stats-based
``estimated`` option, carried through from the source row) and the service-level
flag. So a city served from real listings yields ``real`` options while one that
falls back to per-city statistics yields ``estimated`` options — the estimate path
is never silently presented as firm data.

"Degrade, don't crash" (ADR-005): a destination with neither listings nor price
statistics yields an **empty list**, logged, rather than an error — a missing stay
downgrades a candidate, it does not abort the request.

``ai_engine`` stays free of any backend/ORM import: the DB is reached through the
provider's small ``AccommodationSource`` protocol, which the backend satisfies with
its repositories (see ``backend/db/accommodation_source.py``). This module depends
only on the domain models, the accommodation provider and the standard library.
"""

from __future__ import annotations

import logging

from ai_engine.domain import Accommodation, QualityFlag
from ai_engine.providers.accommodation import (
    AccommodationOption,
    AccommodationRequest,
    AccommodationService,
)
from ai_engine.utils.api_clients import ProviderError, ProviderResult

logger = logging.getLogger(__name__)

# Default and hard-cap number of representative options returned per destination.
# Kept small on purpose — the point is a comparable budget/typical/comfort spread,
# not an exhaustive listing dump (technical-doc §10).
DEFAULT_LIMIT = 3
MAX_LIMIT = 5

# Firmness ranking so an option's flag can be the least-firm of its inputs
# (estimated < cached < real) — mirrors TransportAgent's combination rule.
_QUALITY_RANK = {QualityFlag.ESTIMATED: 0, QualityFlag.CACHED: 1, QualityFlag.REAL: 2}


class StayAgent:
    """Select representative accommodations for a destination (technical-doc §10).

    Constructed with an :class:`AccommodationService` (the DB-backed provider plus
    its price-stats estimate fallback). :meth:`options_for` returns a bounded list
    of normalized :class:`Accommodation` domain models for one destination.
    """

    def __init__(
        self,
        accommodations: AccommodationService,
        *,
        default_limit: int = DEFAULT_LIMIT,
        max_limit: int = MAX_LIMIT,
    ) -> None:
        if not 1 <= default_limit <= max_limit:
            raise ValueError(
                f"default_limit must be within [1, max_limit={max_limit}], got {default_limit}"
            )
        self.accommodations = accommodations
        self.default_limit = default_limit
        self.max_limit = max_limit

    def options_for(
        self,
        destination_id: int,
        *,
        limit: int | None = None,
        currency: str = "eur",
    ) -> list[Accommodation]:
        """Return up to ``limit`` representative :class:`Accommodation`s for a city.

        Resolves representatives from the :class:`AccommodationService` (real
        listings → price-stats estimate), bounds the count to ``[1, max_limit]``,
        and normalizes each into the domain model with the service's
        ``quality_flag``. A destination with no accommodation data at all yields an
        empty list (logged), never an error.
        """
        bound = self._bounded_limit(limit)
        request = AccommodationRequest(
            destination_id=destination_id, limit=bound, currency=currency
        )

        try:
            result: ProviderResult[list[AccommodationOption]] = self.accommodations.resolve(request)
        except ProviderError as exc:
            # No listings and no price statistics for this city — degrade to none.
            logger.warning(
                "stay: no accommodation data for destination %s (%s)", destination_id, exc
            )
            return []

        service_quality = QualityFlag(result.quality_flag)
        bounded = [self._normalize(option, service_quality) for option in (result.data or [])][
            :bound
        ]
        logger.debug(
            "stay: destination %s -> %d representative option(s) [service=%s]",
            destination_id,
            len(bounded),
            service_quality,
        )
        return bounded

    def _bounded_limit(self, limit: int | None) -> int:
        """Clamp a requested option count into ``[1, max_limit]`` (default when None)."""
        requested = self.default_limit if limit is None else limit
        return max(1, min(self.max_limit, requested))

    def _normalize(
        self, option: AccommodationOption, service_quality: QualityFlag
    ) -> Accommodation:
        """Map a provider :class:`AccommodationOption` onto the domain model.

        The single normalization exit point (technical-doc §17/§19): only the
        normalized fields the coordinator needs are carried across — the provider
        label, currency and representative tag do **not** leak into the domain
        shape. The option's ``quality_flag`` is the least-firm of the option's own
        provenance (a real listing vs. a stats-based estimate) and the service-level
        flag, so an estimate is always honestly flagged.
        """
        quality = _worst_quality(service_quality, QualityFlag(option.quality_flag))
        return Accommodation(
            price_per_night=option.price_per_night,
            quality_flag=quality,
            name=option.name,
            accommodation_type=option.accom_type,
            rating=option.rating,
            latitude=option.latitude,
            longitude=option.longitude,
        )


def _worst_quality(*flags: QualityFlag) -> QualityFlag:
    """The least-firm flag among ``flags`` (estimated < cached < real)."""
    return min(flags, key=lambda flag: _QUALITY_RANK[flag])
