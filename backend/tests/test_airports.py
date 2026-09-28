"""AirportIndex nearest-IATA resolution (real-engine wiring)."""

from __future__ import annotations

import json

from ai_engine.utils.airports import Airport, AirportIndex
from ai_engine.utils.geo import GeoPoint

# A tiny fixed set of real hubs, enough to prove nearest-selection + range cutoff.
_SKP = Airport("SKP", "Skopje", GeoPoint(41.9616, 21.6214))
_BCN = Airport("BCN", "Barcelona-El Prat", GeoPoint(41.2971, 2.0785))
_BUD = Airport("BUD", "Budapest Ferenc Liszt", GeoPoint(47.4369, 19.2556))


def test_nearest_returns_closest_hub():
    index = AirportIndex([_SKP, _BCN, _BUD])
    # A point in central Budapest resolves to BUD, not the far-away others.
    assert index.nearest(GeoPoint(47.4979, 19.0402)) == "BUD"
    assert index.nearest(GeoPoint(41.3874, 2.1686)) == "BCN"  # central Barcelona


def test_nearest_respects_max_km():
    index = AirportIndex([_BCN])
    # Skopje is ~1500 km from Barcelona: beyond the default cutoff, so no IATA.
    assert index.nearest(GeoPoint(41.9981, 21.4254), max_km=250.0) is None
    # With the cutoff lifted, it still finds the only airport present.
    assert index.nearest(GeoPoint(41.9981, 21.4254), max_km=None) == "BCN"


def test_empty_index_returns_none():
    assert AirportIndex([]).nearest(GeoPoint(0.0, 0.0)) is None
    assert len(AirportIndex([])) == 0


def test_from_data_dir_missing_file_is_empty(tmp_path):
    # No airports.json in the dir -> empty index (air mode degrades to estimate).
    assert len(AirportIndex.from_data_dir(tmp_path)) == 0


def test_from_data_dir_loads_and_filters(tmp_path):
    (tmp_path / "airports.json").write_text(
        json.dumps(
            {
                "airports": [
                    {"iata": "bud", "name": "Budapest", "lat": 47.4369, "lon": 19.2556},
                    {"iata": "TOOLONG", "name": "bad", "lat": 1.0, "lon": 1.0},  # bad code
                    {"iata": "XXX", "name": "no coords"},  # missing lat/lon
                ]
            }
        ),
        encoding="utf-8",
    )
    index = AirportIndex.from_data_dir(tmp_path)
    assert len(index) == 1  # only the valid row survives
    assert index.nearest(GeoPoint(47.5, 19.05)) == "BUD"  # code upper-cased
