"""Tests for the OpenStreetMap factor ingestion (BS-031).

All offline: the Overpass call (``overpass_query``) is monkeypatched, so these
exercise query construction, the fetch loop's resilience, and the CSV rewrite
(real counts in, ``climate`` preserved) without touching the network.
"""

import csv

import pytest
from scripts import ingest_osm_features as osm


def test_build_query_emits_a_count_per_category():
    query = osm.build_query(42.7, 23.3, 12000)
    # One "out count" per factor, in CATEGORIES order, with the radius/coords.
    assert query.count("out count;") == len(osm.CATEGORIES)
    assert "around:12000,42.7,23.3" in query
    for category in osm.CATEGORIES:
        assert f"->.{category};" in query


def test_fetch_all_maps_counts_to_categories(monkeypatch):
    dests = [
        {
            "city": "Sofia",
            "country": "Bulgaria",
            "country_code": "BG",
            "lat": "42.7",
            "lon": "23.3",
        },
        {"city": "Athens", "country": "Greece", "country_code": "GR", "lat": "38.0", "lon": "23.7"},
    ]
    monkeypatch.setattr(osm, "overpass_query", lambda q, **kw: [10, 20, 30, 40, 50])
    counts, summary = osm.fetch_all(dests, sleep_s=0)
    assert summary.fetched == 2
    assert summary.failed == 0
    assert counts["Sofia|Bulgaria"] == {
        "culture": 10,
        "nature": 20,
        "nightlife": 30,
        "food": 40,
        "tourism": 50,
    }


def test_fetch_all_skips_a_failing_city_but_keeps_the_rest(monkeypatch):
    dests = [
        {"city": "Good", "country": "Land", "country_code": "BG", "lat": "42.7", "lon": "23.3"},
        {"city": "Bad", "country": "Land", "country_code": "BG", "lat": "1.0", "lon": "1.0"},
    ]

    def flaky(query, **kw):
        if "around:12000,1.0,1.0" in query:
            raise RuntimeError("overpass down")
        return [1, 2, 3, 4, 5]

    monkeypatch.setattr(osm, "overpass_query", flaky)
    counts, summary = osm.fetch_all(dests, sleep_s=0)
    assert summary.fetched == 1
    assert summary.failed == 1
    assert "Good|Land" in counts
    assert "Bad|Land" not in counts


def test_regenerate_features_csv_preserves_climate_and_uses_counts(tmp_path):
    features_csv = tmp_path / "features.csv"
    # Pre-existing CSV carries the climate lookup we must preserve.
    features_csv.write_text(
        "city,country,country_code,culture,nature,nightlife,food,tourism,climate\n"
        "Sofia,Bulgaria,BG,1,1,1,1,1,continental\n",
        encoding="utf-8",
    )
    dests = [
        {"city": "Sofia", "country": "Bulgaria", "country_code": "BG", "lat": "42.7", "lon": "23.3"}
    ]
    counts = {
        "Sofia|Bulgaria": {
            "culture": 1370,
            "nature": 1517,
            "nightlife": 336,
            "food": 2971,
            "tourism": 952,
        }
    }

    written = osm.regenerate_features_csv(dests, counts, features_csv=features_csv)
    assert written == 1

    row = next(iter(csv.DictReader(features_csv.open(encoding="utf-8"))))
    assert row["culture"] == "1370"  # real OSM count
    assert row["tourism"] == "952"
    assert row["climate"] == "continental"  # preserved


def test_regenerate_omits_destinations_without_counts(tmp_path):
    features_csv = tmp_path / "features.csv"
    features_csv.write_text(
        "city,country,country_code,culture,nature,nightlife,food,tourism,climate\n"
        "Sofia,Bulgaria,BG,1,1,1,1,1,continental\n",
        encoding="utf-8",
    )
    dests = [
        {
            "city": "Sofia",
            "country": "Bulgaria",
            "country_code": "BG",
            "lat": "42.7",
            "lon": "23.3",
        },
        {"city": "Ghost", "country": "Nowhere", "country_code": "ZZ", "lat": "0", "lon": "0"},
    ]
    counts = {
        "Sofia|Bulgaria": {"culture": 5, "nature": 5, "nightlife": 5, "food": 5, "tourism": 5}
    }
    written = osm.regenerate_features_csv(dests, counts, features_csv=features_csv)
    assert written == 1  # Ghost had no counts -> omitted, never fabricated


def test_read_destinations_missing_file_raises():
    with pytest.raises(FileNotFoundError):
        osm.read_destinations(osm.REPO_ROOT / "data" / "does-not-exist.csv")
