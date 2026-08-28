"""Tests for the Open-Meteo seasonal ingestion (BS-031).

All offline: the network fetch (``fetch_normals``) is monkeypatched, so these
exercise the comfort function, the daily→monthly aggregation, the fetch loop, and
the merge-safe snapshot write without touching the network.
"""

import calendar
import datetime as dt
import json

import pytest
from scripts import ingest_season_normals as season


def test_month_suitability_is_bounded_and_prefers_mild_dry():
    ideal = season.month_suitability(20.0, 0.0)  # perfect temp, no rain
    cold_wet = season.month_suitability(0.0, 150.0)
    hot = season.month_suitability(35.0, 0.0)
    assert ideal == pytest.approx(1.0)
    assert 0.0 <= cold_wet <= 1.0
    assert ideal > hot > cold_wet


def _full_year(year: int, temp_of_month, precip_per_day=1.0):
    times, temps, precs = [], [], []
    d = dt.date(year, 1, 1)
    while d.year == year:
        times.append(d.isoformat())
        temps.append(float(temp_of_month(d.month)))
        precs.append(precip_per_day)
        d += dt.timedelta(days=1)
    return times, temps, precs


def test_monthly_normals_aggregates_temp_and_precip():
    times, temps, precs = _full_year(2020, temp_of_month=lambda m: m, precip_per_day=1.0)
    mean_temp, mean_precip = season.monthly_normals(times, temps, precs)
    assert mean_temp == [float(m) for m in range(1, 13)]
    # precip normal per month = days-in-month * 1mm
    assert mean_precip == [float(calendar.monthrange(2020, m)[1]) for m in range(1, 13)]


def test_monthly_normals_rejects_incomplete_history():
    # Only January present -> other months have no data -> ValueError.
    times = ["2020-01-01", "2020-01-02"]
    with pytest.raises(ValueError, match="incomplete history"):
        season.monthly_normals(times, [5.0, 6.0], [1.0, 1.0])


def test_fetch_all_collects_profiles(monkeypatch):
    dests = [
        {"city": "Sofia", "country": "Bulgaria", "lat": "42.7", "lon": "23.3"},
        {"city": "Athens", "country": "Greece", "lat": "38.0", "lon": "23.7"},
    ]
    fake = {"temp_c": [10.0] * 12, "precip_mm": [30.0] * 12, "suitability": [0.5] * 12}
    monkeypatch.setattr(season, "fetch_normals", lambda lat, lon, s, e: dict(fake))
    profiles, summary = season.fetch_all(dests, sleep_s=0)
    assert summary.fetched == 2 and summary.failed == 0
    assert profiles["Sofia|Bulgaria"]["suitability"] == [0.5] * 12


def test_write_profiles_merges_and_keeps_last_good(tmp_path):
    path = tmp_path / "season.json"
    # Prior snapshot has Athens; only Sofia is fetched fresh this run.
    path.write_text(
        json.dumps(
            {
                "data": [
                    {"city": "Athens", "country": "Greece", "suitability": [0.9] * 12},
                ]
            }
        ),
        encoding="utf-8",
    )
    dests = [
        {"city": "Sofia", "country": "Bulgaria", "lat": "42.7", "lon": "23.3"},
        {"city": "Athens", "country": "Greece", "lat": "38.0", "lon": "23.7"},
        {"city": "Ghost", "country": "Nowhere", "lat": "0", "lon": "0"},
    ]
    fresh = {
        "Sofia|Bulgaria": {"temp_c": [1.0] * 12, "precip_mm": [2.0] * 12, "suitability": [0.4] * 12}
    }

    report = season.write_profiles(dests, fresh, period="2014-2023", path=path)
    assert report.refreshed == ["Sofia"]
    assert report.kept == ["Athens"]  # retained from prior snapshot
    assert report.missing == ["Ghost"]  # no fresh and no prior -> omitted

    written = {(e["city"], e["country"]): e for e in json.loads(path.read_text())["data"]}
    assert written[("Sofia", "Bulgaria")]["suitability"] == [0.4] * 12
    assert written[("Athens", "Greece")]["suitability"] == [0.9] * 12
    assert ("Ghost", "Nowhere") not in written
