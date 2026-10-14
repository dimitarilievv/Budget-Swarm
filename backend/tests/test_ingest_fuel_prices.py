"""Tests for fuel-price ingestion (BS-032).

Two layers:

* **Unit** (no database) — parse the committed fuel-prices.eu snapshots under
  ``data/`` and exercise the resolver: the EU Oil Bulletin export, the /live/
  station summary, range validation, and that every target country (incl. North
  Macedonia) plus the synthetic ``EU`` average row is resolved with a recorded
  ``price_date`` and ``source``.
* **Integration** (``integration`` marker, skipped without a database) — run the
  upsert against a freshly migrated database and assert the refresh routine is
  idempotent: a second run neither duplicates rows nor changes the count.
"""

from __future__ import annotations

from datetime import date
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from config import get_settings
from db.models import FuelPrice
from db.session import SessionLocal
from scripts.ingest_fuel_prices import ingest
from sqlalchemy import create_engine, func, select, text

from scripts import fuel_prices as fp

BACKEND_DIR = Path(__file__).resolve().parents[1]

# Sane consumer fuel-price bounds (EUR/L, tax-incl.) for range assertions.
PRICE_LO, PRICE_HI = 1.0, 3.0


# --- unit: bulletin export parsing -----------------------------------------


def test_bulletin_snapshot_parses():
    result = fp.parse_bulletin_export(fp.load_bulletin_snapshot())
    # The EU Oil Bulletin covers EU-27; the export may omit a member some weeks,
    # so assert a healthy majority rather than exactly 27.
    assert len(result.prices) >= 24
    assert result.price_date == date(2026, 8, 24)
    petrol, diesel = result.eu_average
    assert PRICE_LO <= petrol <= PRICE_HI
    assert PRICE_LO <= diesel <= PRICE_HI


def test_bulletin_prices_are_in_range():
    result = fp.parse_bulletin_export(fp.load_bulletin_snapshot())
    for code, (petrol, diesel) in result.prices.items():
        assert len(code) == 2 and code.isalpha()
        for price in (petrol, diesel):
            assert price is None or PRICE_LO <= price <= PRICE_HI, f"{code}: {price}"


def test_bulletin_export_without_data_date_raises():
    with pytest.raises(ValueError, match="data_date"):
        fp.parse_bulletin_export("no metadata and no table here")


def test_out_of_range_price_is_dropped():
    assert fp._valid_price(1.8) == 1.8
    assert fp._valid_price(99.0) is None
    assert fp._valid_price(-1.0) is None
    assert fp._valid_price(None) is None


# --- unit: /live/ summary parsing ------------------------------------------


def test_live_snapshot_parses_and_covers_north_macedonia():
    prices = fp.parse_live_summary(fp.load_live_snapshot())
    assert "MK" in prices, "North Macedonia should be present in the live feed snapshot"
    petrol, diesel, fetched = prices["MK"]
    assert PRICE_LO <= petrol <= PRICE_HI
    assert PRICE_LO <= diesel <= PRICE_HI
    assert isinstance(fetched, date)


def test_live_summary_bad_shape_raises():
    with pytest.raises(ValueError):
        fp.parse_live_summary({"nope": True})


# --- unit: full resolver ----------------------------------------------------


@pytest.fixture(scope="module")
def resolved() -> fp.ResolvedFuel:
    return fp.resolve_fuel_records(source="snapshot")


def test_every_target_country_incl_nmk_is_resolved(resolved):
    codes = {r.country_code for r in resolved.records}
    for target in fp.DEFAULT_TARGETS:
        assert target in codes, f"missing target {target}"
    assert "MK" in codes  # North Macedonia (NMK) — explicit acceptance criterion
    assert fp.EU_AVG_CODE in codes  # synthetic EU-average row


def test_every_record_records_price_date_and_source(resolved):
    for r in resolved.records:
        assert isinstance(r.price_date, date)
        assert r.source and len(r.source) <= 80
        for price in (r.petrol_e95, r.diesel):
            assert price is None or PRICE_LO <= price <= PRICE_HI


def test_non_eu_neighbours_use_real_live_data(resolved):
    # MK, RS, BA, ME, TR are covered by the live feed → real, not EU-average.
    for code in ("MK", "RS", "BA", "ME", "TR"):
        assert resolved.provenance[code].startswith("live"), code
        rec = next(r for r in resolved.records if r.country_code == code)
        assert rec.source == fp.SOURCE_LIVE


def test_uncovered_country_falls_back_to_eu_average(resolved):
    # Albania has no live coverage → EU-average fallback (documented, flagged).
    assert resolved.provenance["AL"] == "eu-average"
    al = next(r for r in resolved.records if r.country_code == "AL")
    eu = next(r for r in resolved.records if r.country_code == fp.EU_AVG_CODE)
    assert al.petrol_e95 == eu.petrol_e95
    assert al.source == fp.SOURCE_EU_AVG


# --- unit: the pluggable provider layer ------------------------------------


class _FakeProvider:
    """A minimal in-memory provider, standing in for a future source (e.g. US EIA).

    Demonstrates the whole extension contract: give it a ``cc -> (petrol, diesel)``
    mapping and an ``emit_all`` flag, and it plugs straight into
    :func:`resolve_from_providers` with no other change.
    """

    def __init__(self, name: str, source_label: str, prices: dict, *, emit_all: bool) -> None:
        self.name = name
        self.source_label = source_label
        self.emit_all = emit_all
        self._prices = prices

    def load(self) -> fp.ProviderResult:
        return fp.ProviderResult(
            {cc: fp.FuelDatum(p, d, date(2026, 8, 1)) for cc, (p, d) in self._prices.items()},
            loaded_from="test",
        )


def test_custom_provider_plugs_in_and_emits_its_countries():
    # A new region (say the US) is added by writing one provider and listing it.
    us = _FakeProvider("us-eia", "US EIA (test)", {"US": (0.95, 1.05)}, emit_all=True)
    resolved = fp.resolve_from_providers([us], targets=(), fallback=None)

    by = {r.country_code: r for r in resolved.records}
    assert "US" in by
    assert by["US"].source == "US EIA (test)"
    assert by["US"].petrol_e95 == 0.95
    assert resolved.provenance["US"] == "us-eia:test"
    # No EU-average fallback requested → no synthetic EU row.
    assert fp.EU_AVG_CODE not in by


def test_provider_priority_first_wins_and_fill_only_scope():
    # Primary (emit_all) supplies DE; a fill-only provider also lists DE + JP.
    # First provider wins for DE; JP is emitted only because it is a target.
    primary = _FakeProvider("primary", "Primary", {"DE": (2.0, 2.1)}, emit_all=True)
    fill = _FakeProvider("fill", "Fill", {"DE": (9.9, 9.9), "JP": (1.4, 1.5)}, emit_all=False)

    resolved = fp.resolve_from_providers([primary, fill], targets=("JP",), fallback=None)
    by = {r.country_code: r for r in resolved.records}

    assert by["DE"].petrol_e95 == 2.0  # primary won, not the fill provider's 9.9
    assert resolved.provenance["DE"] == "primary:test"
    assert by["JP"].source == "Fill"  # fill-only provider used because JP is a target
    # A fill-only provider's non-target country is not emitted on its own.
    assert set(by) == {"DE", "JP"}


def test_failing_provider_is_skipped_not_fatal():
    class _Boom:
        name, source_label, emit_all = "boom", "Boom", True

        def load(self):
            raise RuntimeError("source down")

    ok = _FakeProvider("ok", "OK", {"DE": (2.0, 2.1)}, emit_all=True)
    resolved = fp.resolve_from_providers([_Boom(), ok], targets=(), fallback=None)
    assert {r.country_code for r in resolved.records} == {"DE"}
    assert "boom" not in resolved.provider_sources


# --- integration: idempotent refresh ---------------------------------------


def _db_reachable(url: str) -> bool:
    try:
        engine = create_engine(url, connect_args={"connect_timeout": 2})
        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))
        return True
    except Exception:
        return False


def _alembic_config() -> Config:
    cfg = Config(str(BACKEND_DIR / "alembic.ini"))
    cfg.set_main_option("script_location", str(BACKEND_DIR / "alembic"))
    return cfg


@pytest.fixture
def migrated_engine():
    url = get_settings().database_url
    if not _db_reachable(url):
        pytest.skip("no reachable database — integration test skipped")

    cfg = _alembic_config()
    command.downgrade(cfg, "base")
    command.upgrade(cfg, "head")

    engine = create_engine(url)
    try:
        yield engine
    finally:
        engine.dispose()
        command.downgrade(cfg, "base")


@pytest.mark.integration
def test_ingest_populates_and_refreshes_idempotently(migrated_engine):
    """First run inserts every resolved row; a second run is a no-op on count."""
    session = SessionLocal(bind=migrated_engine)
    try:
        first, records = ingest(session, source="snapshot")
        session.commit()
        assert first.inserted == first.resolved
        assert first.updated == 0

        count_after_first = session.scalar(select(func.count()).select_from(FuelPrice))
        assert count_after_first == len(records)

        # North Macedonia (NMK) row is present with a price_date + source.
        mk = session.get(FuelPrice, "MK")
        assert mk is not None
        assert mk.price_date is not None
        assert mk.source == fp.SOURCE_LIVE

        # Re-run: idempotent — no new rows, everything counted as an update.
        second, _ = ingest(session, source="snapshot")
        session.commit()
        assert second.inserted == 0
        assert second.updated == second.resolved

        count_after_second = session.scalar(select(func.count()).select_from(FuelPrice))
        assert count_after_second == count_after_first
    finally:
        session.close()
