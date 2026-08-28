"""Tests for city geocoding via Open-Meteo (BS-029 helper).

Offline: ``urlopen`` is monkeypatched, and payload parsing is tested directly.
"""

import json

import pytest
from scripts import geocode


class _FakeResponse:
    def __init__(self, payload):
        self._payload = payload

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def read(self):
        return json.dumps(self._payload).encode()


_BARCELONA = {
    "results": [
        {
            "name": "Barcelona",
            "country": "Spain",
            "country_code": "ES",
            "latitude": 41.38879,
            "longitude": 2.15899,
            "population": 1686208,
            "admin1": "Catalonia",
        }
    ]
}


def test_parse_results_extracts_valid_row():
    results = geocode._parse_results(_BARCELONA)
    assert len(results) == 1
    r = results[0]
    assert (r.city, r.country, r.country_code) == ("Barcelona", "Spain", "ES")
    assert r.as_destination_row() == {
        "city": "Barcelona",
        "country": "Spain",
        "country_code": "ES",
        "lat": "41.3888",  # rounded to 4 dp
        "lon": "2.159",
    }
    assert "Catalonia" in r.label


@pytest.mark.parametrize(
    "payload",
    [
        {},
        {"results": []},
        {"results": [{"name": "X", "country": "Y", "latitude": 1, "longitude": 2}]},  # no code
        {
            "results": [
                {"name": "X", "country": "Y", "country_code": "ESP", "latitude": 1, "longitude": 2}
            ]
        },
        {
            "results": [
                {"name": "X", "country": "Y", "country_code": "ES", "latitude": 999, "longitude": 2}
            ]
        },
    ],
)
def test_parse_results_skips_invalid(payload):
    assert geocode._parse_results(payload) == []


def test_geocode_returns_ranked_results(monkeypatch):
    monkeypatch.setattr(
        geocode.urllib.request, "urlopen", lambda req, timeout=0: _FakeResponse(_BARCELONA)
    )
    results = geocode.geocode("Barcelona")
    assert results[0].country_code == "ES"
    assert geocode.best_match("Barcelona").city == "Barcelona"


def test_geocode_empty_name_raises():
    with pytest.raises(ValueError, match="non-empty"):
        geocode.geocode("   ")


def test_geocode_network_failure_raises_runtime(monkeypatch):
    def boom(req, timeout=0):
        raise TimeoutError("down")

    monkeypatch.setattr(geocode.urllib.request, "urlopen", boom)
    with pytest.raises(RuntimeError, match="lookup failed"):
        geocode.geocode("Barcelona")


def test_best_match_none_when_no_results(monkeypatch):
    monkeypatch.setattr(
        geocode.urllib.request, "urlopen", lambda req, timeout=0: _FakeResponse({"results": []})
    )
    assert geocode.best_match("Nowhereville") is None
