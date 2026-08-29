"""Pluggable accommodation-source layer (BS-033).

Accommodation data can come from several places — Inside Airbnb today, and a
different open dataset (a Booking.com export, a tourism-board feed, …) tomorrow.
Every source implements one small contract, :class:`AccommodationSource`, and is
loaded verbatim into the source-agnostic ``raw_accommodation_listings`` table:
the whole CSV row is kept as JSONB, so a source's own (wildly different) schema
never needs a schema change here. BS-034 reads that table and normalizes the
usable rows into ``accommodations``.

Adding a source is: subclass :class:`CsvSnapshotSource` (or implement the
protocol directly), give it a ``name`` + ``required_columns`` + a ``discover()``,
and register it in :data:`SOURCES`. The importer (``scripts.ingest_accommodations``)
and the table stay untouched.

Shared machinery lives here — a streamed, cached, atomic downloader and a
memory-bounded gzip/plain CSV chunk reader with header validation — so a typical
"download a CSV snapshot per city" source only has to describe *where* its files
are, not *how* to fetch and read them.
"""

from __future__ import annotations

import gzip
import logging
import urllib.request
from collections.abc import Iterator
from csv import DictReader
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Protocol, runtime_checkable

logger = logging.getLogger(__name__)

# backend/scripts/accommodation_sources.py -> repo root is two parents up.
REPO_ROOT = Path(__file__).resolve().parents[2]
CACHE_ROOT = REPO_ROOT / "data"


@dataclass(frozen=True)
class DatasetRef:
    """A single downloadable snapshot for one city from one source."""

    source: str  # source name, e.g. 'inside-airbnb'
    city: str  # city slug, e.g. 'riga'
    snapshot_date: date
    url: str
    meta: dict[str, str] = field(default_factory=dict)  # source-specific (country/region…)

    @property
    def snapshot_label(self) -> str:
        """Stable per-row label, e.g. ``inside-airbnb-riga-2026-06-30`` (<= 60 chars)."""
        return f"{self.source}-{self.city}-{self.snapshot_date.isoformat()}"

    @property
    def cache_name(self) -> str:
        return f"{self.source}-{self.city}-{self.snapshot_date.isoformat()}-listings.csv.gz"


@runtime_checkable
class AccommodationSource(Protocol):
    """The contract every accommodation source implements.

    ``name`` identifies the source (stored on every row and used in provenance);
    ``required_columns`` are the columns downstream normalization needs, validated
    against each snapshot's header. ``discover`` resolves the current snapshot per
    requested city; ``download`` fetches one snapshot to a local cache; and
    ``iter_chunks`` streams it as memory-bounded batches of dict rows.
    """

    name: str
    required_columns: frozenset[str]

    def default_targets(self) -> tuple[str, ...]: ...

    def discover(self, *, targets: tuple[str, ...], timeout: float) -> dict[str, DatasetRef]: ...

    def download(
        self, ref: DatasetRef, *, cache_dir: Path, timeout: float, force: bool
    ) -> Path: ...

    def iter_chunks(self, path: Path, *, chunk_size: int) -> Iterator[list[dict[str, str]]]: ...


# --- shared machinery -------------------------------------------------------


def stream_download(
    ref: DatasetRef, *, cache_dir: Path, timeout: float = 120.0, force: bool = False
) -> Path:
    """Download ``ref``'s gzipped CSV into ``cache_dir`` (skips if already cached).

    Streamed to a temp file and atomically renamed, so an interrupted download
    never leaves a truncated file a later run would trust.
    """
    cache_dir.mkdir(parents=True, exist_ok=True)
    target = cache_dir / ref.cache_name
    if target.is_file() and not force:
        logger.info("%s: %s already cached (%d bytes)", ref.source, ref.city, target.stat().st_size)
        return target

    logger.info("%s: downloading %s snapshot %s", ref.source, ref.city, ref.snapshot_date)
    tmp = target.with_suffix(target.suffix + ".part")
    request = urllib.request.Request(ref.url, headers={"Accept": "application/gzip"})
    with urllib.request.urlopen(request, timeout=timeout) as response:  # noqa: S310 (https only)
        with tmp.open("wb") as fh:
            while chunk := response.read(1 << 16):
                fh.write(chunk)
    tmp.replace(target)
    logger.info("%s: saved %s (%d bytes)", ref.source, target.name, target.stat().st_size)
    return target


def _is_gzip(path: Path) -> bool:
    """True if ``path`` starts with the gzip magic bytes (regardless of suffix)."""
    with path.open("rb") as fh:
        return fh.read(2) == b"\x1f\x8b"


def iter_csv_chunks(
    path: Path, *, required_columns: frozenset[str], chunk_size: int = 5000
) -> Iterator[list[dict[str, str]]]:
    """Stream a listings CSV (gzipped or plain) as chunks of dict rows.

    Memory-bounded: at most ``chunk_size`` rows are held at once. Raises
    ``ValueError`` if a required column is missing from the header, so schema
    drift is caught before any rows are loaded.
    """
    opener = gzip.open if _is_gzip(path) else open
    with opener(path, "rt", encoding="utf-8", newline="") as fh:
        reader = DictReader(fh)
        missing = set(required_columns) - set(reader.fieldnames or [])
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


class CsvSnapshotSource:
    """Base for sources that publish a downloadable CSV snapshot per city.

    Provides the shared ``download``/``iter_chunks`` (streamed cache + validated
    chunk reader); a concrete source sets ``name``/``required_columns`` and
    implements ``default_targets`` and ``discover``.
    """

    name: str = "csv-snapshot"
    required_columns: frozenset[str] = frozenset()

    def default_targets(self) -> tuple[str, ...]:  # pragma: no cover - overridden
        raise NotImplementedError

    def discover(
        self, *, targets: tuple[str, ...], timeout: float
    ) -> dict[str, DatasetRef]:  # pragma: no cover - overridden
        raise NotImplementedError

    def download(
        self,
        ref: DatasetRef,
        *,
        cache_dir: Path | None = None,
        timeout: float = 120.0,
        force: bool = False,
    ) -> Path:
        cache_dir = cache_dir or (CACHE_ROOT / self.name)
        return stream_download(ref, cache_dir=cache_dir, timeout=timeout, force=force)

    def iter_chunks(self, path: Path, *, chunk_size: int = 5000) -> Iterator[list[dict[str, str]]]:
        return iter_csv_chunks(path, required_columns=self.required_columns, chunk_size=chunk_size)


def get_source(name: str) -> AccommodationSource:
    """Return the registered source by name (raises ``KeyError`` if unknown)."""
    sources = available_sources()
    if name not in sources:
        raise KeyError(
            f"unknown accommodation source {name!r}; known: {', '.join(sorted(sources))}"
        )
    return sources[name]


def available_sources() -> dict[str, AccommodationSource]:
    """The source registry. Lazy import keeps this module dependency-light."""
    from scripts.inside_airbnb import InsideAirbnbSource

    return {InsideAirbnbSource.name: InsideAirbnbSource()}
