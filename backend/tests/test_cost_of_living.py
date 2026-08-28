"""Tests for the cost-of-living provider abstraction (BS-011 / BS-031).

All offline (no network, no database): they exercise the generic CSV provider,
the priority merge across providers, the regional-average fallback, and that a
dead provider is skipped rather than fatal. WhereNext's own provider adapter is
covered by ``test_wherenext.py``.
"""

from pathlib import Path

import pytest
from scripts.cost_of_living import (
    EUROPEAN_SUBREGIONS,
    CostOfLivingProvider,
    CsvCostOfLivingProvider,
    ProviderResult,
    resolve_cost_index,
)


class _StaticProvider:
    """A trivial in-memory provider for tests."""

    def __init__(self, name: str, mapping: dict[str, float]):
        self.name = name
        self._mapping = mapping

    def load(self) -> ProviderResult:
        return ProviderResult(dict(self._mapping), self.name, {})


class _DeadProvider:
    name = "dead"

    def load(self) -> ProviderResult:
        raise RuntimeError("source unavailable")


# --------------------------------------------------------------------------- #
# CSV provider
# --------------------------------------------------------------------------- #


def test_csv_provider_reads_country_code_and_cost_index(tmp_path):
    path = tmp_path / "src.csv"
    path.write_text(
        "country_code,cost_index,note\nBG,32,cheap\nat,72,pricey\nXX,-1,bad\n",
        encoding="utf-8",
    )
    result = CsvCostOfLivingProvider(path, name="kaggle").load()
    assert result.source == "csv:kaggle"
    assert result.mapping == {"BG": 32.0, "AT": 72.0}  # uppercased; negative dropped


def test_csv_provider_missing_file_raises():
    with pytest.raises(FileNotFoundError):
        CsvCostOfLivingProvider(Path("nope.csv")).load()


def test_csv_provider_missing_column_raises(tmp_path):
    path = tmp_path / "bad.csv"
    path.write_text("country_code,price\nBG,32\n", encoding="utf-8")
    with pytest.raises(ValueError, match="cost_index"):
        CsvCostOfLivingProvider(path).load()


def test_providers_satisfy_the_protocol(tmp_path):
    path = tmp_path / "s.csv"
    path.write_text("country_code,cost_index\nBG,32\n", encoding="utf-8")
    assert isinstance(CsvCostOfLivingProvider(path), CostOfLivingProvider)
    assert isinstance(_StaticProvider("x", {}), CostOfLivingProvider)


# --------------------------------------------------------------------------- #
# resolve_cost_index — priority, fallback, resilience
# --------------------------------------------------------------------------- #


def test_first_provider_wins_on_conflict():
    primary = _StaticProvider("primary", {"BG": 30.0})
    secondary = _StaticProvider("secondary", {"BG": 99.0, "PL": 35.0})
    resolved = resolve_cost_index([primary, secondary])
    assert resolved.mapping["BG"] == 30.0  # primary wins
    assert resolved.mapping["PL"] == 35.0  # filled by secondary
    assert resolved.provenance["BG"] == "primary"
    assert resolved.provenance["PL"] == "secondary"
    assert resolved.provider_labels == ["primary", "secondary"]


def test_dead_provider_is_skipped_not_fatal():
    resolved = resolve_cost_index([_DeadProvider(), _StaticProvider("live", {"BG": 30.0})])
    assert resolved.mapping == {"BG": 30.0}
    assert resolved.provider_labels == ["live"]


def test_regional_average_fills_uncovered_country():
    # Primary covers two Balkan countries; a third Balkan country is filled with
    # their average, and a needed country in an unknown region stays missing.
    primary = _StaticProvider("primary", {"BG": 30.0, "RS": 40.0})
    needed = {"BG", "RS", "ME", "ZZ"}  # ME=Balkans (fillable), ZZ=unknown
    resolved = resolve_cost_index([primary], needed=needed, region_by_country=EUROPEAN_SUBREGIONS)
    assert resolved.mapping["ME"] == pytest.approx(35.0)  # mean(30, 40)
    assert resolved.provenance["ME"] == "regional-average:balkans"
    assert "ZZ" not in resolved.mapping  # no region -> no fill
    assert "regional-average" in resolved.provider_labels


def test_no_regional_fill_without_region_map():
    primary = _StaticProvider("primary", {"BG": 30.0})
    resolved = resolve_cost_index([primary], needed={"BG", "ME"})
    assert "ME" not in resolved.mapping  # no region map supplied
