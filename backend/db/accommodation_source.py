"""Wire the accommodation provider's DB reads to the repositories (BS-060).

StayAgent's :class:`~ai_engine.providers.accommodation.AccommodationService` reads
the database through the small ``AccommodationSource`` protocol
(``list_by_destination`` + ``price_stats``), so ``ai_engine`` never imports the ORM.
This module is the backend bridge: :class:`RepositoryAccommodationSource` adapts the
``AccommodationRepository`` and ``AccommodationPriceStatsRepository`` to that
protocol, and :func:`build_accommodation_service` assembles the DB-backed provider
plus its price-stats estimate fallback for a request scope.
"""

from __future__ import annotations

import logging

from sqlalchemy.orm import Session

from ai_engine.providers.accommodation import (
    AccommodationService,
    InsideAirbnbProvider,
    PriceStatsEstimateProvider,
)
from db.models import Accommodation, AccommodationPriceStats
from db.repositories import AccommodationPriceStatsRepository, AccommodationRepository

logger = logging.getLogger(__name__)


class RepositoryAccommodationSource:
    """Adapt the accommodation repositories to the provider's ``AccommodationSource``.

    ``list_by_destination`` returns listings ordered by price (the repository
    already orders them); ``price_stats`` returns the precomputed distribution row
    or ``None``. Both ORM models satisfy the provider's duck-typed row protocols.
    """

    def __init__(self, session: Session) -> None:
        self._listings = AccommodationRepository(session)
        self._stats = AccommodationPriceStatsRepository(session)

    def list_by_destination(self, destination_id: int) -> list[Accommodation]:
        return self._listings.list_by_destination(destination_id)

    def price_stats(self, destination_id: int) -> AccommodationPriceStats | None:
        return self._stats.get_for_destination(destination_id)


def build_accommodation_service(session: Session) -> AccommodationService:
    """Assemble the DB-backed :class:`AccommodationService` for one request scope.

    The primary reads the ``accommodations`` table; the fallback synthesizes options
    from ``accommodation_price_stats`` (BS-035) for cities without listings.
    """
    source = RepositoryAccommodationSource(session)
    return AccommodationService(
        primary=InsideAirbnbProvider(source),
        fallback=PriceStatsEstimateProvider(source),
    )
