"""Unit tests for the Inside Airbnb refresh workflow (offline, no DB/network).

Cover the decision core: when a snapshot is considered new vs unchanged, the
validation rules that reject suspicious/incomplete snapshots, bounded download
retries, and the check-mode orchestration (detect new, skip unchanged, skip
not-in-index) via fakes. The full import+rollback-on-failure path reuses the
existing, DB-backed ingest/normalize pipeline (exercised by the live verification
and the accommodation integration tests), so it is not re-run here.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date

import pytest
import scripts.refresh_accommodations as refresh
from scripts.normalize_accommodations import NormalizeSummary, PriceStats


@dataclass
class _Ref:
    city: str
    snapshot_date: date
    snapshot_label: str


def _ref(city: str, d: str) -> _Ref:
    return _Ref(city, date.fromisoformat(d), f"inside-airbnb-{city}-{d}")


# --- needs_refresh -----------------------------------------------------------


def test_unchanged_snapshot_is_skipped():
    r = _ref("athens", "2026-09-22")
    assert refresh.needs_refresh(r.snapshot_label, "inside-airbnb", r) == (False, "unchanged")


def test_newer_snapshot_triggers_refresh():
    r = _ref("athens", "2026-12-20")
    do, _ = refresh.needs_refresh("inside-airbnb-athens-2026-09-22", "inside-airbnb", r)
    assert do is True


def test_older_index_snapshot_is_not_imported():
    r = _ref("athens", "2026-06-01")
    do, _ = refresh.needs_refresh("inside-airbnb-athens-2026-09-22", "inside-airbnb", r)
    assert do is False


def test_city_with_no_real_stats_is_refreshed():
    r = _ref("athens", "2026-09-22")
    assert refresh.needs_refresh(None, None, r)[0] is True
    # An estimate row (not inside-airbnb) must not block a real import.
    assert refresh.needs_refresh("wherenext-estimate-x", "wherenext-cost-model", r)[0] is True


# --- validate ----------------------------------------------------------------


_OK_STATS = PriceStats(200, 10.0, 60.0, 90.0, 140.0, 500.0)


def _summary(normalized=200, stats=_OK_STATS, error=None):
    return NormalizeSummary(
        snapshot_label="inside-airbnb-athens-2026-09-22",
        city="athens",
        destination_id=1,
        normalized=normalized,
        stats=stats,
        error=error,
    )


def test_valid_snapshot_passes():
    assert refresh.validate(_summary(), min_listings=50) is None


def test_too_few_listings_rejected():
    assert "listings" in refresh.validate(_summary(normalized=10), min_listings=50)


def test_missing_stats_rejected():
    assert refresh.validate(_summary(stats=None), min_listings=50) == "no price statistics"


def test_non_positive_or_nonfinite_stats_rejected():
    bad = PriceStats(200, 0.0, -1.0, 90.0, 140.0, 500.0)
    assert refresh.validate(_summary(stats=bad), min_listings=50)


def test_unordered_stats_rejected():
    bad = PriceStats(200, 10.0, 140.0, 90.0, 60.0, 500.0)  # q1 > median > q3
    assert "ordered" in refresh.validate(_summary(stats=bad), min_listings=50)


def test_upstream_error_is_a_rejection():
    assert refresh.validate(_summary(error="boom"), min_listings=50) == "boom"


# --- download retry ----------------------------------------------------------


class _FlakySource:
    name = "inside-airbnb"

    def __init__(self, fail_times):
        self.calls = 0
        self._fail_times = fail_times

    def download(self, ref, *, timeout, force):
        self.calls += 1
        if self.calls <= self._fail_times:
            raise OSError("network blip")
        return "/tmp/snapshot.csv.gz"


def test_download_retries_then_succeeds(monkeypatch):
    monkeypatch.setattr(refresh.time, "sleep", lambda *_: None)
    src = _FlakySource(fail_times=1)
    path = refresh._download_with_retry(
        src, _ref("athens", "2026-09-22"), timeout=5, retries=2, force=False
    )
    assert path == "/tmp/snapshot.csv.gz" and src.calls == 2


def test_download_raises_after_exhausting_retries(monkeypatch):
    monkeypatch.setattr(refresh.time, "sleep", lambda *_: None)
    src = _FlakySource(fail_times=5)
    with pytest.raises(OSError):
        refresh._download_with_retry(
            src, _ref("athens", "2026-09-22"), timeout=5, retries=2, force=False
        )
    assert src.calls == 2


# --- check-mode orchestration (fakes, no DB/network) -------------------------


@dataclass
class _Row:
    snapshot_label: str
    source: str


class _FakeSource:
    name = "inside-airbnb"

    def __init__(self, refs):
        self._refs = refs

    def default_targets(self):
        return tuple(self._refs)

    def currency_for(self, city):
        return "EUR"

    def discover(self, *, targets, timeout):
        return {c: self._refs[c] for c in targets if c in self._refs}


class _FakeSession:
    def __init__(self, rows):
        self._rows = rows  # destination_id -> _Row | None

    def rollback(self):
        pass

    def get(self, _model, pk):
        return self._rows.get(pk)

    def close(self):
        pass


def test_check_mode_detects_new_unchanged_and_missing(monkeypatch):
    refs = {
        "athens": _ref("athens", "2026-12-20"),  # newer than current -> new
        "budapest": _ref("budapest", "2026-09-21"),  # equal to current -> unchanged
        "vienna": _ref("vienna", "2026-09-17"),  # no current row -> new
        # warsaw intentionally absent from the index
    }
    ids = {"athens": 1, "budapest": 2, "vienna": 3, "warsaw": 4}
    rows = {
        1: _Row("inside-airbnb-athens-2026-09-22", "inside-airbnb"),
        2: _Row("inside-airbnb-budapest-2026-09-21", "inside-airbnb"),
        3: None,
    }
    monkeypatch.setattr(refresh, "InsideAirbnbSource", lambda: _FakeSource(refs))
    monkeypatch.setattr(refresh, "resolve_destination_id", lambda _s, city: ids.get(city))
    monkeypatch.setattr(refresh, "SessionLocal", lambda bind=None: _FakeSession(rows))
    monkeypatch.setattr(refresh, "get_engine", lambda: None)
    monkeypatch.setattr("scripts.currency.load_eur_rates", lambda source: None)

    outcomes = {
        o.city: o.action
        for o in refresh.run_refresh(
            cities=("athens", "budapest", "vienna", "warsaw"), check_only=True
        )
    }
    assert outcomes == {
        "athens": "new-available",
        "budapest": "unchanged",
        "vienna": "new-available",
        "warsaw": "skipped",
    }
