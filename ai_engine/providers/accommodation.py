"""AccommodationProvider: DB-backed lodging + price-stats estimate (BS-050; §21/§22).

Unlike the transport/flight adapters, accommodation is not fetched live per
request: Inside Airbnb snapshots are ingested, validated and normalized into the
``accommodations`` table, and per-city price distributions are precomputed into
``accommodation_price_stats`` (BS-033/034/035). This provider therefore reads
BudgetSwarm's own database — the DB *is* the cache (technical-doc §23) — and
returns representative options for a destination. For a city with no listings it
degrades to an ``estimated`` option synthesized from that city's price statistics
(§22 "degrade, don't crash"), so StayAgent always has a number.

Decoupling: the DB is reached through a tiny :class:`AccommodationSource` protocol
(``list_by_destination`` + ``price_stats``) that the backend's
``AccommodationRepository`` / ``AccommodationPriceStatsRepository`` satisfy
structurally, so ``ai_engine`` carries no SQLAlchemy or backend dependency and is
unit-tested with fakes. Rows are duck-typed on the ORM attribute names
(``price_per_night``, ``rating``, …) and mapped to the internal
:class:`AccommodationOption` here.
"""

from __future__ import annotations

import logging
from abc import ABC
from dataclasses import dataclass
from typing import Any, Protocol, runtime_checkable

from ai_engine.utils.api_clients import (
    QUALITY_ESTIMATED,
    QUALITY_REAL,
    BaseProvider,
    EstimateProvider,
    ProviderNoResult,
)

logger = logging.getLogger(__name__)


@runtime_checkable
class AccommodationRow(Protocol):
    """Duck-typed normalized accommodation row (the ORM model satisfies this)."""

    name: str | None
    accom_type: str | None
    price_per_night: Any  # Decimal in the ORM; coerced to float here
    rating: Any | None
    latitude: float | None
    longitude: float | None
    quality_flag: str


@runtime_checkable
class PriceStatsRow(Protocol):
    """Duck-typed per-destination price distribution (BS-035)."""

    sample_size: int
    currency: str
    price_min: Any | None
    price_q1: Any | None
    price_median: Any | None
    price_q3: Any | None
    price_max: Any | None


class AccommodationSource(Protocol):
    """The DB reads this provider needs (kept backend-agnostic).

    ``list_by_destination`` returns rows ordered by price; ``price_stats`` returns
    the precomputed distribution or ``None``. A thin backend adapter maps these to
    ``AccommodationRepository.list_by_destination`` and
    ``AccommodationPriceStatsRepository.get_for_destination``.
    """

    def list_by_destination(self, destination_id: int) -> list[AccommodationRow]: ...

    def price_stats(self, destination_id: int) -> PriceStatsRow | None: ...


@dataclass(frozen=True)
class AccommodationRequest:
    """A lodging query for one destination; ``limit`` caps representative options."""

    destination_id: int
    limit: int = 3
    currency: str = "eur"


@dataclass
class AccommodationOption:
    """Normalized lodging option — the internal shape StayAgent consumes."""

    price_per_night: float
    currency: str
    provider: str  # provenance label
    name: str | None = None
    accom_type: str | None = None
    rating: float | None = None
    latitude: float | None = None
    longitude: float | None = None
    representative: str | None = None  # 'budget' | 'typical' | 'comfort'
    # Per-option provenance carried from the source row (a real listing) or the
    # estimate provider ('estimated'), so honesty survives normalization (BS-062).
    quality_flag: str = QUALITY_REAL


def _to_float(value: Any) -> float | None:
    return None if value is None else float(value)


def _representative_indices(count: int, limit: int) -> list[int]:
    """Pick up to ``limit`` evenly-spaced indices across a price-sorted list.

    With few rows every row is kept; with many, the spread samples the price range
    (cheapest → mid → priciest) rather than only the cheapest few.
    """
    if count <= limit:
        return list(range(count))
    if limit == 1:
        return [0]
    step = (count - 1) / (limit - 1)
    return sorted({round(i * step) for i in range(limit)})


class AccommodationProvider(BaseProvider[AccommodationRequest, list[AccommodationOption]], ABC):
    """Interface for a source of representative accommodation options."""

    name = "accommodation"
    cache_type = "accommodation"  # long-lived — snapshot data, refreshed by re-ingest (§23)


class InsideAirbnbProvider(AccommodationProvider):
    """Primary provider: representative options from the normalized ``accommodations``
    table (Inside Airbnb; BS-033/034/035).

    Returns an empty list when the destination has no listings, so the fallback
    chain (or the caller) can degrade to the price-stats estimate.
    """

    name = "inside-airbnb"

    def __init__(self, source: AccommodationSource, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self._source = source

    def _fetch(self, request: AccommodationRequest) -> list[AccommodationOption]:
        rows = self._source.list_by_destination(request.destination_id) or []
        if not rows:
            logger.info("accommodation: no listings for destination %s", request.destination_id)
            return []
        labels = ["budget", "typical", "comfort"]
        idxs = _representative_indices(len(rows), request.limit)
        options: list[AccommodationOption] = []
        for pos, i in enumerate(idxs):
            row = rows[i]
            options.append(
                AccommodationOption(
                    price_per_night=_to_float(row.price_per_night),
                    currency=request.currency.lower(),
                    provider=self.name,
                    name=row.name,
                    accom_type=row.accom_type,
                    rating=_to_float(row.rating),
                    latitude=row.latitude,
                    longitude=row.longitude,
                    representative=labels[pos] if len(idxs) <= len(labels) else None,
                    quality_flag=getattr(row, "quality_flag", None) or QUALITY_REAL,
                )
            )
        return options


class PriceStatsEstimateProvider(EstimateProvider[AccommodationRequest, list[AccommodationOption]]):
    """Fallback: synthesize options from a city's price statistics (BS-035).

    Uses Q1 / median / Q3 (budget / typical / comfort) when present, else the
    median alone. Every option is flagged ``estimated``. A destination with no
    stats row raises :class:`ProviderNoResult` so the chain fails honestly rather
    than inventing a price.
    """

    name = "price-stats-estimate"

    def __init__(self, source: AccommodationSource, *, name: str | None = None) -> None:
        super().__init__(name=name)
        self._source = source

    def _estimate(self, request: AccommodationRequest) -> list[AccommodationOption]:
        stats = self._source.price_stats(request.destination_id)
        if stats is None:
            raise ProviderNoResult(f"no price statistics for destination {request.destination_id}")
        tiers = [
            ("budget", _to_float(stats.price_q1)),
            ("typical", _to_float(stats.price_median)),
            ("comfort", _to_float(stats.price_q3)),
        ]
        present = [(label, price) for label, price in tiers if price is not None]
        if not present:
            median = _to_float(stats.price_median)
            if median is None:
                raise ProviderNoResult(
                    f"price statistics for destination {request.destination_id} have no usable price"
                )
            present = [("typical", median)]
        currency = (getattr(stats, "currency", None) or request.currency).lower()
        return [
            AccommodationOption(
                price_per_night=price,
                currency=currency,
                provider=self.name,
                accom_type="estimate",
                representative=label,
                quality_flag=QUALITY_ESTIMATED,
            )
            for label, price in present
        ][: request.limit]


@dataclass
class AccommodationService:
    """Compose the DB provider with the price-stats estimate (§22 chain).

    ``resolve(request)`` returns real listings when the city has them, else the
    ``estimated`` stats-based options — always a non-empty list with an honest
    ``quality_flag`` when any data exists for the destination.
    """

    primary: AccommodationProvider
    fallback: PriceStatsEstimateProvider

    def resolve(self, request: AccommodationRequest):
        from ai_engine.utils.api_clients import fetch_with_fallback

        return fetch_with_fallback([self.primary], request, estimate=self.fallback._estimate)
