"""Tests for adding cities to the candidate list by name (BS-029 helper).

Offline: ``geocode.best_match`` is monkeypatched so no network is used.
"""

import csv

from scripts import add_destination
from scripts.geocode import GeocodeResult


def _match(city, country, code, lat, lon):
    return GeocodeResult(city, country, code, lat, lon)


def _seed(path):
    path.write_text(
        "city,country,country_code,lat,lon\nSofia,Bulgaria,BG,42.6977,23.3219\n",
        encoding="utf-8",
    )


def test_add_new_city_appends_row(tmp_path, monkeypatch):
    csv_path = tmp_path / "dest.csv"
    _seed(csv_path)
    monkeypatch.setattr(
        add_destination.geocode,
        "best_match",
        lambda name: _match("Barcelona", "Spain", "ES", 41.38879, 2.15899),
    )

    report = add_destination.add_cities(["Barcelona"], path=csv_path)
    assert report.added == ["Barcelona, Spain"]
    assert report.skipped_existing == []
    assert report.not_found == []

    rows = list(csv.DictReader(csv_path.open(encoding="utf-8")))
    assert [r["city"] for r in rows] == ["Sofia", "Barcelona"]
    added = rows[-1]
    assert added["country_code"] == "ES"
    assert added["lat"] == "41.3888"  # rounded to 4 dp


def test_existing_city_is_skipped(tmp_path, monkeypatch):
    csv_path = tmp_path / "dest.csv"
    _seed(csv_path)
    monkeypatch.setattr(
        add_destination.geocode,
        "best_match",
        lambda name: _match("Sofia", "Bulgaria", "BG", 42.7, 23.3),
    )
    report = add_destination.add_cities(["Sofia"], path=csv_path)
    assert report.skipped_existing == ["Sofia, Bulgaria"]
    assert report.added == []
    # File unchanged: still exactly one data row.
    assert len(list(csv.DictReader(csv_path.open(encoding="utf-8")))) == 1


def test_unresolvable_name_reported_not_found(tmp_path, monkeypatch):
    csv_path = tmp_path / "dest.csv"
    _seed(csv_path)
    monkeypatch.setattr(add_destination.geocode, "best_match", lambda name: None)
    report = add_destination.add_cities(["Xyzzyville"], path=csv_path)
    assert report.not_found == ["Xyzzyville"]
    assert report.added == []


def test_dry_run_does_not_write(tmp_path, monkeypatch):
    csv_path = tmp_path / "dest.csv"
    _seed(csv_path)
    monkeypatch.setattr(
        add_destination.geocode,
        "best_match",
        lambda name: _match("Lyon", "France", "FR", 45.749, 4.847),
    )
    report = add_destination.add_cities(["Lyon"], path=csv_path, dry_run=True)
    assert report.added == ["Lyon, France"]
    assert len(list(csv.DictReader(csv_path.open(encoding="utf-8")))) == 1  # not written


def test_duplicate_within_one_batch_added_once(tmp_path, monkeypatch):
    csv_path = tmp_path / "dest.csv"
    _seed(csv_path)
    monkeypatch.setattr(
        add_destination.geocode,
        "best_match",
        lambda name: _match("Porto", "Portugal", "PT", 41.15, -8.61),
    )
    report = add_destination.add_cities(["Porto", "porto"], path=csv_path)
    assert report.added == ["Porto, Portugal"]
    assert report.skipped_existing == ["Porto, Portugal"]  # second one deduped
