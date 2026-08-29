"""Inside Airbnb dataset discovery + download (BS-033 / BS-007).

Inside Airbnb (insideairbnb.com) publishes quarterly ``listings.csv.gz`` snapshots
per city — the primary accommodation source decided in BS-007 (free, no key, CC BY
4.0 with CC0 on the data). This module resolves the current snapshot for each
target city from the public "get the data" index, downloads the raw gzipped CSV
into a local cache, and streams its rows in chunks (files are tens of MB / tens of
thousands of rows).

Nothing here cleans or normalizes: the importer (``scripts.ingest_airbnb``) stores
the rows verbatim in ``airbnb_listings_raw``; BS-034 does the validation and
normalization into ``accommodations``. Downloaded files live under ``data/`` (git
-ignored — large downloads are never committed); the committed artifact is this
code plus a tiny excerpt fixture used by the tests.

Only the target cities Inside Airbnb actually covers appear here. The Balkan/CEE
cities in the curated set (Sofia, Belgrade, Skopje, …) are not published by Inside
Airbnb and are handled later as ``estimated`` (BS-007 decision, BS-034/035).

Attribution: "Inside Airbnb (insideairbnb.com), CC BY 4.0".
"""

from __future__ import annotations

import gzip
import logging
import re
import urllib.request
from collections.abc import Iterator
from csv import DictReader
from dataclasses import dataclass
from datetime import date
from pathlib import Path

logger = logging.getLogger(__name__)

# backend/scripts/inside_airbnb.py -> repo root is two parents up.
REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_CACHE_DIR = REPO_ROOT / "data" / "inside_airbnb"

INDEX_URL = "https://insideairbnb.com/get-the-data/"
# Inside Airbnb data URLs are https://data.insideairbnb.com/<country>/<region>/<city>/<date>/data/listings.csv.gz
_LISTINGS_RE = re.compile(
    r"https://data\.insideairbnb\.com/([^/]+)/([^/]+)/([^/]+)/"
    r"(\d{4}-\d{2}-\d{2})/data/listings\.csv\.gz"
)

# Target cities Inside Airbnb covers (subset of data/curated_destinations.csv,
# verified against the get-the-data index on 2026-08-29). Slugs are the city path
# segment used by Inside Airbnb.
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

# Columns BS-034 relies on to normalize a listing; the import validates their
# presence in each snapshot's header (a schema drift here should fail loudly).
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


@dataclass(frozen=True)
class DatasetRef:
    """A single downloadable Inside Airbnb snapshot for one city."""

    city: str
    snapshot_date: date
    url: str
    country: str
    region: str

    @property
    def snapshot_label(self) -> str:
        """Stable label stored on every row, e.g. ``inside-airbnb-riga-2026-06-30``."""
        return f"inside-airbnb-{self.city}-{self.snapshot_date.isoformat()}"

    @property
    def cache_name(self) -> str:
        return f"{self.city}-{self.snapshot_date.isoformat()}-listings.csv.gz"


def fetch_index(timeout: float = 30.0) -> str:
    """GET the Inside Airbnb "get the data" index HTML (raises on network error)."""
    logger.info("inside-airbnb: fetching dataset index from %s", INDEX_URL)
    request = urllib.request.Request(INDEX_URL, headers={"Accept": "text/html"})
    with urllib.request.urlopen(request, timeout=timeout) as response:  # noqa: S310 (https only)
        return response.read().decode("utf-8", errors="replace")


def parse_index(html: str, *, targets: tuple[str, ...] = TARGET_CITIES) -> dict[str, DatasetRef]:
    """Resolve the latest ``listings.csv.gz`` snapshot per target city from the index.

    Returns ``{city: DatasetRef}`` for the target cities found; a target absent
    from the index is logged and simply omitted (coverage changes over time).
    """
    wanted = set(targets)
    latest: dict[str, DatasetRef] = {}
    for country, region, city, iso in _LISTINGS_RE.findall(html):
        if city not in wanted:
            continue
        ref = DatasetRef(
            city, date.fromisoformat(iso), _rebuild_url(country, region, city, iso), country, region
        )
        current = latest.get(city)
        if current is None or ref.snapshot_date > current.snapshot_date:
            latest[city] = ref

    for missing in sorted(wanted - set(latest)):
        logger.warning("inside-airbnb: no snapshot found in index for target city %r", missing)
    return latest


def _rebuild_url(country: str, region: str, city: str, iso: str) -> str:
    return f"https://data.insideairbnb.com/{country}/{region}/{city}/{iso}/data/listings.csv.gz"


def discover_datasets(
    *, targets: tuple[str, ...] = TARGET_CITIES, timeout: float = 30.0
) -> dict[str, DatasetRef]:
    """Fetch + parse the index into ``{city: DatasetRef}`` for the target cities."""
    return parse_index(fetch_index(timeout), targets=targets)


def download(
    ref: DatasetRef,
    *,
    cache_dir: Path = DEFAULT_CACHE_DIR,
    timeout: float = 120.0,
    force: bool = False,
) -> Path:
    """Download ``ref``'s gzipped CSV into ``cache_dir`` (skips if already cached).

    Downloads are streamed to a temporary file and atomically renamed, so an
    interrupted download never leaves a truncated file that a later run trusts.
    """
    cache_dir.mkdir(parents=True, exist_ok=True)
    target = cache_dir / ref.cache_name
    if target.is_file() and not force:
        logger.info("inside-airbnb: %s already cached (%d bytes)", ref.city, target.stat().st_size)
        return target

    logger.info("inside-airbnb: downloading %s snapshot %s", ref.city, ref.snapshot_date)
    tmp = target.with_suffix(target.suffix + ".part")
    request = urllib.request.Request(ref.url, headers={"Accept": "application/gzip"})
    with urllib.request.urlopen(request, timeout=timeout) as response:  # noqa: S310 (https only)
        with tmp.open("wb") as fh:
            while chunk := response.read(1 << 16):
                fh.write(chunk)
    tmp.replace(target)
    logger.info("inside-airbnb: saved %s (%d bytes)", target.name, target.stat().st_size)
    return target


def validate_header(header: list[str]) -> set[str]:
    """Return the required columns missing from ``header`` (empty = all present)."""
    return set(REQUIRED_COLUMNS) - set(header)


def _is_gzip(path: Path) -> bool:
    """True if ``path`` starts with the gzip magic bytes (regardless of suffix)."""
    with path.open("rb") as fh:
        return fh.read(2) == b"\x1f\x8b"


def iter_chunks(path: Path, *, chunk_size: int = 5000) -> Iterator[list[dict[str, str]]]:
    """Stream a listings CSV (gzipped or plain) as chunks of dict rows.

    Memory-bounded: only ``chunk_size`` rows are held at a time. Raises
    ``ValueError`` if required columns are missing from the header, so a schema
    drift is caught before any rows are loaded.
    """
    opener = gzip.open if _is_gzip(path) else open
    with opener(path, "rt", encoding="utf-8", newline="") as fh:
        reader = DictReader(fh)
        missing = validate_header(reader.fieldnames or [])
        if missing:
            raise ValueError(
                f"{path.name} is missing required column(s): {', '.join(sorted(missing))}"
            )
        chunk: list[dict[str, str]] = []
        for row in reader:
            chunk.append(row)
            if len(chunk) >= chunk_size:
                yield chunk
                chunk = []
        if chunk:
            yield chunk
