"""Cost-of-living provider abstraction (BS-011 / BS-031).

Cost-of-living can come from several sources. The BS-011 decision names
**WhereNext** as primary, the Kaggle "Global Cost of Living" dataset as a
fallback for countries WhereNext does not cover, and a **regional average** as
the always-available floor (the same degrade-don't-crash pattern as fuel, and
the layered fallback chain of ADR-004/ADR-005).

To keep those sources swappable, every source implements one tiny contract —
``CostOfLivingProvider`` — that returns a ``country_code -> cost_index`` mapping.
Nothing downstream (normalization, the join to destinations, the DB write) knows
or cares which source produced the numbers. Adding a new source is: write one
class with a ``load()`` method; it is then usable anywhere a provider is.

``resolve_cost_index`` composes providers in priority order (first provider to
supply a country wins) and, for any still-missing country that the caller needs,
fills a **regional average** computed from the countries already resolved in the
same region. This is what turns "WhereNext primary + a fallback" from a plan into
one line of wiring.
"""

from __future__ import annotations

import csv
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol, runtime_checkable

logger = logging.getLogger(__name__)


@dataclass
class ProviderResult:
    """What a single cost-of-living provider returns."""

    mapping: dict[str, float]  # ISO alpha-2 country code -> cost_index
    source: str  # provenance label, e.g. "wherenext:live" or "csv:kaggle"
    meta: dict[str, object] = field(default_factory=dict)


@runtime_checkable
class CostOfLivingProvider(Protocol):
    """The contract every cost-of-living source implements.

    ``name`` is a short identifier; ``load()`` returns a :class:`ProviderResult`.
    A provider that cannot supply data (missing file, network down) should raise;
    :func:`resolve_cost_index` logs and skips to the next provider.
    """

    name: str

    def load(self) -> ProviderResult: ...


class CsvCostOfLivingProvider:
    """A provider backed by a ``country_code,cost_index`` CSV file.

    This is the drop-in slot for any tabular source — e.g. a normalized export of
    the Kaggle "Global Cost of Living" dataset (BS-011 fallback), or a hand-curated
    override table. Point it at a CSV with ``country_code`` and ``cost_index``
    columns; other columns are ignored.
    """

    def __init__(self, path: Path, *, name: str = "csv") -> None:
        self.path = Path(path)
        self.name = name

    def load(self) -> ProviderResult:
        if not self.path.is_file():
            raise FileNotFoundError(f"cost-of-living CSV not found: {self.path}")
        mapping: dict[str, float] = {}
        with self.path.open(encoding="utf-8", newline="") as fh:
            reader = csv.DictReader(fh)
            missing = {"country_code", "cost_index"} - set(reader.fieldnames or [])
            if missing:
                raise ValueError(
                    f"{self.path.name} is missing column(s): {', '.join(sorted(missing))}"
                )
            for row in reader:
                code = str(row.get("country_code", "")).strip().upper()
                if len(code) != 2 or not code.isalpha():
                    continue
                try:
                    index = float(str(row.get("cost_index", "")).strip())
                except ValueError:
                    continue
                if index >= 0:
                    mapping[code] = index
        if not mapping:
            raise ValueError(f"{self.path.name} contained no usable cost_index rows")
        return ProviderResult(mapping, f"csv:{self.name}", {"path": str(self.path)})


@dataclass
class ResolvedCost:
    """The merged result of resolving several providers."""

    mapping: dict[str, float]  # country_code -> cost_index (merged)
    provenance: dict[str, str]  # country_code -> the label that supplied it
    provider_labels: list[str]  # provenance labels that contributed, in order


# Coarse European sub-regions used only for the regional-average fallback. Kept
# small and static on purpose; a country absent here simply gets no regional
# fill (its cost_of_living stays NULL and is logged).
EUROPEAN_SUBREGIONS: dict[str, str] = {
    # Balkans
    "AL": "balkans",
    "BA": "balkans",
    "BG": "balkans",
    "HR": "balkans",
    "ME": "balkans",
    "MK": "balkans",
    "RS": "balkans",
    "SI": "balkans",
    "XK": "balkans",
    "TR": "balkans",
    # Baltics
    "EE": "baltics",
    "LT": "baltics",
    "LV": "baltics",
    # Central Europe
    "AT": "central-europe",
    "CH": "central-europe",
    "CZ": "central-europe",
    "DE": "central-europe",
    "HU": "central-europe",
    "PL": "central-europe",
    "SK": "central-europe",
    # Eastern Europe
    "MD": "eastern-europe",
    "RO": "eastern-europe",
    "UA": "eastern-europe",
    # Southern Europe
    "CY": "southern-europe",
    "ES": "southern-europe",
    "GR": "southern-europe",
    "IT": "southern-europe",
    "MT": "southern-europe",
    "PT": "southern-europe",
    # Western Europe
    "BE": "western-europe",
    "FR": "western-europe",
    "GB": "western-europe",
    "IE": "western-europe",
    "LU": "western-europe",
    "NL": "western-europe",
    # Northern Europe
    "DK": "northern-europe",
    "FI": "northern-europe",
    "IS": "northern-europe",
    "NO": "northern-europe",
    "SE": "northern-europe",
}


def resolve_cost_index(
    providers: list[CostOfLivingProvider],
    *,
    needed: set[str] | None = None,
    region_by_country: dict[str, str] | None = None,
) -> ResolvedCost:
    """Merge ``providers`` in priority order, then regional-average-fill the gaps.

    The first provider to supply a country wins (so order = priority). If
    ``needed`` and ``region_by_country`` are given, any needed country still
    missing is filled with the mean cost of the already-resolved countries in the
    same region — the BS-011 "regional average" floor. Providers that raise are
    logged and skipped, so one dead source never breaks the chain.
    """
    mapping: dict[str, float] = {}
    provenance: dict[str, str] = {}
    labels: list[str] = []

    for provider in providers:
        try:
            result = provider.load()
        except Exception as exc:  # noqa: BLE001 - degrade, never crash (ADR-005)
            logger.warning("cost provider %s failed: %s", getattr(provider, "name", provider), exc)
            continue
        labels.append(result.source)
        for code, value in result.mapping.items():
            if code not in mapping:  # earlier provider wins
                mapping[code] = value
                provenance[code] = result.source

    if needed and region_by_country:
        by_region: dict[str, list[float]] = {}
        for code, value in mapping.items():
            region = region_by_country.get(code)
            if region:
                by_region.setdefault(region, []).append(value)

        filled_any = False
        for code in needed:
            if code in mapping:
                continue
            region = region_by_country.get(code)
            values = by_region.get(region) if region else None
            if values:
                mapping[code] = sum(values) / len(values)
                provenance[code] = f"regional-average:{region}"
                filled_any = True
        if filled_any:
            labels.append("regional-average")

    return ResolvedCost(mapping, provenance, labels)
