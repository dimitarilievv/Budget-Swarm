"""Unit tests for the OurAirports → snapshot parser (scripts.ingest_airports).

Covers the filtering rules that keep the airport snapshot to viable, priceable
commercial endpoints — in particular the ``scheduled_service`` requirement that drops
a closed/cargo-only hub (e.g. Istanbul Atatürk / ``ISL``) so the nearest-airport lookup
never returns a code the flight API cannot price.
"""

from __future__ import annotations

from scripts.ingest_airports import parse_airports

# A minimal CSV with the columns parse_airports reads, one row per case.
_HEADER = "type,iata_code,latitude_deg,longitude_deg,name,scheduled_service"
_ROWS = [
    # kept: large airport, IATA, scheduled service
    "large_airport,IST,41.27487,28.73214,Istanbul Airport,yes",
    # dropped: large airport but NO scheduled service (closed/cargo — the ISL case)
    "large_airport,ISL,40.97191,28.82371,Istanbul Ataturk Airport,no",
    # kept: medium airport with scheduled service
    "medium_airport,SAW,40.8986,29.3092,Sabiha Gokcen,yes",
    # dropped: small airport (wrong type) even with scheduled service
    "small_airport,XXA,10.0,10.0,Tiny Strip,yes",
    # dropped: no IATA code
    "large_airport,,50.0,8.0,No Code Field,yes",
    # dropped: bad coordinates
    "medium_airport,BAD,999.0,8.0,Bad Coords,yes",
]


def _csv(*rows: str) -> str:
    return "\n".join([_HEADER, *rows]) + "\n"


def test_parse_keeps_only_serviced_commercial_airports():
    records = parse_airports(_csv(*_ROWS))
    codes = {r["iata"] for r in records}
    assert codes == {"IST", "SAW"}  # ISL (no service), the strip, the code-less and bad rows gone


def test_parse_drops_unscheduled_hub():
    # The specific regression: a closed hub with scheduled_service=no must not survive, so the
    # nearest-airport lookup can't return a code the flight API rejects with a 4xx.
    records = parse_airports(_csv("large_airport,ISL,40.97,28.82,Istanbul Ataturk,no"))
    assert records == []


def test_parse_treats_missing_scheduled_service_as_not_serviced():
    # A blank scheduled_service column is treated as "no" (conservative — only keep confirmed
    # commercial endpoints).
    records = parse_airports(_csv("large_airport,ZZZ,10.0,10.0,Unknown Service,"))
    assert records == []
