"""Unit tests for the multi-city calendar refresh orchestrator (offline, mocked boundaries).

Cover: city targeting, the scheduled enable/disable gate, non-zero exit when any city fails,
and run_refresh's skip-unchanged / not-in-index / download-failure / import paths. The DB and
network boundaries are mocked; the atomic validate-before-replace behaviour is exercised against
a real Postgres in the real-data validation.
"""

from __future__ import annotations

from argparse import Namespace
from datetime import date

import scripts.refresh_calendars as refresh
from scripts.inside_airbnb import InsideAirbnbSource
from scripts.refresh_calendars import CityResult


class _Ref:
    def __init__(self, city, iso):
        self.city = city
        self.snapshot_date = date.fromisoformat(iso)
        self.snapshot_label = f"inside-airbnb-{city}-{iso}"
        self.url = f"https://data.insideairbnb.com/x/y/{city}/{iso}/data/listings.csv.gz"


class _FakeSource:
    name = "inside-airbnb"

    def __init__(self, refs):
        self._refs = refs

    def default_targets(self):
        return tuple(self._refs)

    def currency_for(self, _city):
        return "EUR"

    def discover(self, *, targets, timeout):
        return {c: self._refs[c] for c in targets if c in self._refs}


class _Ctx:
    def __enter__(self):
        return self

    def __exit__(self, *_a):
        return False


# --- _parse_cities -----------------------------------------------------------


def test_parse_cities_single_all_and_list():
    src = InsideAirbnbSource()
    assert refresh._parse_cities(Namespace(city="Budapest", cities=None, all=False), src) == (
        "budapest",
    )
    assert refresh._parse_cities(Namespace(city=None, cities="riga, prague", all=False), src) == (
        "riga",
        "prague",
    )
    assert refresh._parse_cities(Namespace(city=None, cities=None, all=True), src) == (
        src.default_targets()
    )


# --- main: scheduler gate + exit codes ---------------------------------------


def test_scheduled_is_noop_when_disabled(monkeypatch):
    calls = []
    monkeypatch.delenv("ACCOMMODATION_CALENDAR_REFRESH_ENABLED", raising=False)
    monkeypatch.setattr(refresh, "run_refresh", lambda *a, **k: calls.append(1) or [])
    assert refresh.main(["--all", "--scheduled"]) == 0
    assert calls == []  # the disabled scheduler never runs the refresh


def test_scheduled_runs_when_enabled(monkeypatch):
    monkeypatch.setenv("ACCOMMODATION_CALENDAR_REFRESH_ENABLED", "true")
    monkeypatch.setattr(refresh, "run_refresh", lambda *a, **k: [CityResult("riga", "ok")])
    assert refresh.main(["--all", "--scheduled"]) == 0


def test_exit_nonzero_when_any_city_fails(monkeypatch):
    monkeypatch.setattr(
        refresh,
        "run_refresh",
        lambda *a, **k: [CityResult("riga", "ok"), CityResult("prague", "failed", "boom")],
    )
    assert refresh.main(["--all"]) == 1


def test_exit_zero_when_all_ok_or_skipped(monkeypatch):
    monkeypatch.setattr(
        refresh,
        "run_refresh",
        lambda *a, **k: [CityResult("riga", "ok"), CityResult("prague", "skipped", "unchanged")],
    )
    assert refresh.main(["--all"]) == 0


# --- run_refresh orchestration (mocked discover/download/ingest) --------------


def _wire(monkeypatch, refs, *, stored, download_fail=(), refresh_status="ok"):
    monkeypatch.setattr(refresh, "InsideAirbnbSource", lambda: _FakeSource(refs))
    monkeypatch.setattr("scripts.currency.load_eur_rates", lambda source: None)
    monkeypatch.setattr(refresh, "get_engine", lambda: None)
    monkeypatch.setattr(refresh, "sessionmaker", lambda bind=None: (lambda: _Ctx()))
    monkeypatch.setattr(refresh, "_stored_snapshot", lambda _s, c: stored.get(c))

    def _dl(url, target, *, timeout):
        city = next(c for c in refs if f"/{c}/" in url)
        if city in download_fail:
            raise OSError("network down")
        return f"/tmp/{city}.csv.gz"

    monkeypatch.setattr(refresh, "_download", _dl)
    monkeypatch.setattr(
        refresh,
        "refresh_city",
        lambda sf, source, rates, city, ref, path, **k: CityResult(
            city, refresh_status, rows_stored=100
        ),
    )


def test_run_refresh_skips_unchanged_imports_new_and_flags_missing(monkeypatch):
    refs = {"riga": _Ref("riga", "2026-10-01"), "prague": _Ref("prague", "2026-10-01")}
    _wire(monkeypatch, refs, stored={"riga": "inside-airbnb-riga-2026-10-01"})  # riga unchanged
    results = {r.city: r.status for r in refresh.run_refresh(("riga", "prague", "vienna"))}
    assert results == {"riga": "skipped", "prague": "ok", "vienna": "failed"}  # vienna not in index


def test_run_refresh_isolates_download_failure(monkeypatch):
    refs = {"riga": _Ref("riga", "2026-10-01"), "prague": _Ref("prague", "2026-10-01")}
    _wire(monkeypatch, refs, stored={}, download_fail=("prague",))
    results = {r.city: r.status for r in refresh.run_refresh(("riga", "prague"))}
    assert results["riga"] == "ok" and results["prague"] == "failed"
