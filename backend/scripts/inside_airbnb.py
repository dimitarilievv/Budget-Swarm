"""Inside Airbnb accommodation source (BS-033 / BS-007).

Inside Airbnb (insideairbnb.com) publishes quarterly ``listings.csv.gz`` snapshots
per city — the primary accommodation source decided in BS-007 (free, no key, CC BY
4.0 with CC0 on the data). This module implements the
:class:`~scripts.accommodation_sources.AccommodationSource` contract for it: it
resolves the current snapshot for each target city from the public "get the data"
index; downloading and chunked reading come from the shared base
(:class:`~scripts.accommodation_sources.CsvSnapshotSource`).

Nothing here cleans or normalizes: the importer stores rows verbatim in
``raw_accommodation_listings``; BS-034 normalizes into ``accommodations``.

Only the target cities Inside Airbnb actually covers are listed. The Balkan/CEE
cities in the curated set (Sofia, Belgrade, Skopje, …) are not published by Inside
Airbnb and are handled later as ``estimated`` (BS-007 decision, BS-034/035).

Attribution: "Inside Airbnb (insideairbnb.com), CC BY 4.0".
"""

from __future__ import annotations

import logging
import re
import urllib.request
from datetime import date

from scripts.accommodation_sources import CsvSnapshotSource, DatasetRef

logger = logging.getLogger(__name__)

INDEX_URL = "https://insideairbnb.com/get-the-data/"
# Inside Airbnb data URLs: https://data.insideairbnb.com/<country>/<region>/<city>/<date>/data/listings.csv.gz
_LISTINGS_RE = re.compile(
    r"https://data\.insideairbnb\.com/([^/]+)/([^/]+)/([^/]+)/"
    r"(\d{4}-\d{2}-\d{2})/data/listings\.csv\.gz"
)

# Target cities Inside Airbnb covers (subset of data/curated_destinations.csv,
# verified against the get-the-data index on 2026-08-29).
TARGET_CITIES: tuple[str, ...] = (
    "athens",
    "budapest",
    "istanbul",
    "lisbon",
    "naples",
    "porto",
    "prague",
    "riga",
    "thessaloniki",
    "valencia",
    "vienna",
)

# Columns BS-034 relies on to normalize a listing; presence is validated per snapshot.
REQUIRED_COLUMNS: frozenset[str] = frozenset(
    {
        "id",
        "price",
        "price_quote_price_per_night",
        "room_type",
        "property_type",
        "accommodates",
        "latitude",
        "longitude",
        "minimum_nights",
    }
)

LICENSE = "CC BY 4.0"
ATTRIBUTION = "Inside Airbnb (insideairbnb.com)"

# Local currency each covered city's prices are quoted in. Inside Airbnb reports
# prices in the listing's market currency, and the per-row currency field is
# unreliable (often null, e.g. all of Prague), so it is fixed per city here.
CITY_CURRENCY: dict[str, str] = {
    "athens": "EUR",
    "budapest": "HUF",
    "istanbul": "TRY",
    "lisbon": "EUR",
    "naples": "EUR",
    "porto": "EUR",
    "prague": "CZK",
    "riga": "EUR",
    "thessaloniki": "EUR",
    "valencia": "EUR",
    "vienna": "EUR",
}


def _rebuild_url(country: str, region: str, city: str, iso: str) -> str:
    return f"https://data.insideairbnb.com/{country}/{region}/{city}/{iso}/data/listings.csv.gz"


class InsideAirbnbSource(CsvSnapshotSource):
    """Inside Airbnb as a pluggable :class:`AccommodationSource`."""

    name = "inside-airbnb"
    required_columns = REQUIRED_COLUMNS
    index_url = INDEX_URL

    def default_targets(self) -> tuple[str, ...]:
        return TARGET_CITIES

    def currency_for(self, city: str) -> str:
        currency = CITY_CURRENCY.get(city.lower())
        if currency is None:
            logger.warning("inside-airbnb: unknown currency for city %r; assuming EUR", city)
            return "EUR"
        return currency

    def fetch_index(self, timeout: float = 30.0) -> str:
        """GET the Inside Airbnb "get the data" index HTML (raises on network error)."""
        logger.info("inside-airbnb: fetching dataset index from %s", self.index_url)
        request = urllib.request.Request(self.index_url, headers={"Accept": "text/html"})
        with urllib.request.urlopen(
            request, timeout=timeout
        ) as response:  # noqa: S310 (https only)
            return response.read().decode("utf-8", errors="replace")

    def parse_index(
        self, html: str, *, targets: tuple[str, ...] = TARGET_CITIES
    ) -> dict[str, DatasetRef]:
        """Resolve the latest ``listings.csv.gz`` snapshot per target city.

        Returns ``{city: DatasetRef}`` for the target cities found; a target absent
        from the index is logged and omitted (coverage changes over time).
        """
        wanted = set(targets)
        latest: dict[str, DatasetRef] = {}
        for country, region, city, iso in _LISTINGS_RE.findall(html):
            if city not in wanted:
                continue
            ref = DatasetRef(
                source=self.name,
                city=city,
                snapshot_date=date.fromisoformat(iso),
                url=_rebuild_url(country, region, city, iso),
                meta={"country": country, "region": region},
            )
            current = latest.get(city)
            if current is None or ref.snapshot_date > current.snapshot_date:
                latest[city] = ref

        for missing in sorted(wanted - set(latest)):
            logger.warning("inside-airbnb: no snapshot found in index for target city %r", missing)
        return latest

    def discover(
        self, *, targets: tuple[str, ...] = TARGET_CITIES, timeout: float = 30.0
    ) -> dict[str, DatasetRef]:
        """Fetch + parse the index into ``{city: DatasetRef}`` for the target cities."""
        return self.parse_index(self.fetch_index(timeout), targets=targets)
