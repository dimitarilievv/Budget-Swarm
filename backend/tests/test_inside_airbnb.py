"""Inside Airbnb source: coverage-integrity + snapshot-discovery tests (BS-033/BS-007).

Fast unit tests — no network, no database. They guard two things:

* **Coverage integrity:** every ``TARGET_CITIES`` slug resolves to a real curated
  destination (so a typo can't silently drop a covered city) and has a currency
  in ``CITY_CURRENCY`` (so the per-city currency is never guessed at runtime).
* **Discovery behaviour:** ``parse_index`` keeps the latest snapshot per target,
  ignores non-target cities, and omits a target that is absent from the index
  (missing data is skipped, never faked).

Coverage note (verified against the live insideairbnb.com get-the-data index on
2026-10-14): of the 30 curated destinations, Inside Airbnb publishes exactly 11 —
the ones listed in ``TARGET_CITIES``. The other 19 are not published by Inside
Airbnb at all, so they have no accommodation listings/statistics and are correctly
excluded by the optimizer (``missing_accommodation``) rather than given invented
prices. Snapshots are periodic point-in-time exports, not live/bookable prices.
"""

import csv
from pathlib import Path

from scripts.inside_airbnb import CITY_CURRENCY, TARGET_CITIES, InsideAirbnbSource
from scripts.normalize_accommodations import CITY_SLUG_OVERRIDES

REPO_ROOT = Path(__file__).resolve().parents[2]
CURATED_CSV = REPO_ROOT / "data" / "curated_destinations.csv"


def _curated_city_names() -> set[str]:
    with CURATED_CSV.open(encoding="utf-8", newline="") as fh:
        return {row["city"].strip().lower() for row in csv.DictReader(fh)}


def _slug_to_destination_name(slug: str) -> str:
    """Mirror resolve_destination_id's slug->city transform (normalize step)."""
    return CITY_SLUG_OVERRIDES.get(slug.lower(), slug.replace("-", " "))


# --- coverage integrity ------------------------------------------------------


def test_target_cities_are_the_expected_eleven():
    # Pins the verified coverage: expanding requires adding a city Inside Airbnb
    # actually publishes (re-verify against the live index first).
    assert set(TARGET_CITIES) == {
        "athens",
        "budapest",
        "istanbul",
        "lisbon",
        "naples",
        "porto",
        "prague",
        "riga",
        "thessaloniki",
        "valencia",
        "vienna",
    }


def test_every_target_resolves_to_a_curated_destination():
    curated = _curated_city_names()
    for slug in TARGET_CITIES:
        name = _slug_to_destination_name(slug)
        assert name.lower() in curated, f"target {slug!r} -> {name!r} is not a curated destination"


def test_every_target_has_a_currency():
    for slug in TARGET_CITIES:
        assert slug in CITY_CURRENCY, f"no CITY_CURRENCY entry for target {slug!r}"
        currency = CITY_CURRENCY[slug]
        assert len(currency) == 3 and currency.isalpha() and currency.isupper()


def test_no_stray_currency_entries():
    # Every currency mapping should correspond to an actual target (no dead entries).
    assert set(CITY_CURRENCY) == set(TARGET_CITIES)


# --- snapshot discovery (no network: parse a crafted index) ------------------

_FAKE_INDEX = """
<a href="https://data.insideairbnb.com/greece/attica/athens/2026-06-01/data/listings.csv.gz">old</a>
<a href="https://data.insideairbnb.com/greece/attica/athens/2026-09-22/data/listings.csv.gz">new</a>
<a href="https://data.insideairbnb.com/czech-republic/prague/prague/2026-09-20/data/listings.csv.gz">x</a>
<a href="https://data.insideairbnb.com/france/ile-de-france/paris/2026-09-01/data/listings.csv.gz">x</a>
"""


def test_parse_index_keeps_latest_snapshot_per_target():
    refs = InsideAirbnbSource().parse_index(_FAKE_INDEX, targets=("athens", "prague"))
    assert set(refs) == {"athens", "prague"}
    assert refs["athens"].snapshot_date.isoformat() == "2026-09-22"  # newer wins
    assert refs["athens"].snapshot_label == "inside-airbnb-athens-2026-09-22"
    assert refs["athens"].url.endswith("/athens/2026-09-22/data/listings.csv.gz")


def test_parse_index_ignores_non_target_cities():
    refs = InsideAirbnbSource().parse_index(_FAKE_INDEX, targets=("athens",))
    assert "paris" not in refs and "prague" not in refs


def test_parse_index_omits_target_absent_from_index():
    # A target with no snapshot in the index is skipped (not invented).
    refs = InsideAirbnbSource().parse_index(_FAKE_INDEX, targets=("athens", "sofia"))
    assert "sofia" not in refs and "athens" in refs


def test_parse_index_percent_encodes_non_ascii_region():
    # Budapest's region ("közép-magyarország") is non-ASCII; the built URL must be
    # ASCII-safe (percent-encoded UTF-8) so urllib can request it without crashing.
    html = (
        '<a href="https://data.insideairbnb.com/hungary/'
        'közép-magyarország/budapest/2026-09-21/data/listings.csv.gz">x</a>'
    )
    refs = InsideAirbnbSource().parse_index(html, targets=("budapest",))
    url = refs["budapest"].url
    assert url.isascii()  # safe for urllib (the original bug was a UnicodeEncodeError)
    assert "k%C3%B6z%C3%A9p-magyarorsz%C3%A1g" in url
    assert url.endswith("/budapest/2026-09-21/data/listings.csv.gz")
