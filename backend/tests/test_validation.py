"""Unit tests for the reusable structured validation utilities (BS-036).

Each validator is exercised on valid and invalid inputs; the results are the
structured ``Issue``/``RowResult`` objects, not bare booleans.
"""

from __future__ import annotations

from dataclasses import FrozenInstanceError

import pytest
from scripts.validation import FieldSpec, Issue

from scripts import validation as val

SPECS = (
    FieldSpec("city", required=True),
    FieldSpec("country", required=True),
    FieldSpec("country_code", kind="iso2"),
    FieldSpec("lat", required=True, kind="float", min=-90.0, max=90.0),
    FieldSpec("lon", required=True, kind="float", min=-180.0, max=180.0),
)


# --- check_schema -----------------------------------------------------------


def test_check_schema_reports_missing_columns():
    issues = val.check_schema(["city", "lat"], ("city", "country", "lat"))
    assert [i.field for i in issues] == ["country"]
    assert all(i.code == "schema" for i in issues)


def test_check_schema_all_present_is_empty():
    assert val.check_schema(["a", "b", "c"], ("a", "b")) == []


def test_check_schema_handles_none_fieldnames():
    issues = val.check_schema(None, ("a",))
    assert [i.field for i in issues] == ["a"]


# --- validate_row -----------------------------------------------------------


def test_validate_row_valid_row_normalizes():
    result = val.validate_row(
        {
            "city": " Sofia ",
            "country": "Bulgaria",
            "country_code": "bg",
            "lat": "42.7",
            "lon": "23.3",
        },
        SPECS,
    )
    assert result.ok
    assert result.issues == []
    assert result.values == {
        "city": "Sofia",
        "country": "Bulgaria",
        "country_code": "BG",  # iso2 uppercased
        "lat": 42.7,
        "lon": 23.3,
    }


def test_validate_row_optional_blank_is_none_without_issue():
    result = val.validate_row(
        {"city": "X", "country": "Y", "country_code": "", "lat": "1", "lon": "2"}, SPECS
    )
    assert result.ok
    assert result.values["country_code"] is None


@pytest.mark.parametrize(
    "row,bad_field,code",
    [
        ({"city": "", "country": "Y", "lat": "1", "lon": "2"}, "city", "missing"),
        ({"city": "X", "country": "", "lat": "1", "lon": "2"}, "country", "missing"),
        ({"city": "X", "country": "Y", "lat": "n/a", "lon": "2"}, "lat", "type"),
        ({"city": "X", "country": "Y", "lat": "", "lon": "2"}, "lat", "missing"),
        ({"city": "X", "country": "Y", "lat": "99", "lon": "2"}, "lat", "range"),
        ({"city": "X", "country": "Y", "lat": "1", "lon": "999"}, "lon", "range"),
        (
            {"city": "X", "country": "Y", "country_code": "PRT", "lat": "1", "lon": "2"},
            "country_code",
            "type",
        ),
        (
            {"city": "X", "country": "Y", "country_code": "12", "lat": "1", "lon": "2"},
            "country_code",
            "type",
        ),
    ],
)
def test_validate_row_flags_each_bad_shape(row, bad_field, code):
    result = val.validate_row(row, SPECS)
    assert not result.ok
    assert any(i.field == bad_field and i.code == code for i in result.issues), result.issues


def test_validate_row_reason_joins_issues():
    result = val.validate_row({"city": "", "country": "", "lat": "1", "lon": "2"}, SPECS)
    assert "city:" in result.reason and "country:" in result.reason


def test_validate_row_choices():
    specs = (FieldSpec("mode", required=True, choices=frozenset({"car", "bus"})),)
    assert val.validate_row({"mode": "car"}, specs).ok
    bad = val.validate_row({"mode": "plane"}, specs)
    assert not bad.ok
    assert bad.issues[0].code == "choice"


def test_validate_row_money_kind_and_range():
    specs = (FieldSpec("price", required=True, kind="money", min=1.0, max=1000.0),)
    assert val.validate_row({"price": "$40.00"}, specs).values["price"] == 40.0
    assert val.validate_row({"price": "$0"}, specs).issues[0].code == "range"
    assert val.validate_row({"price": ""}, specs).issues[0].code == "missing"


# --- detect_duplicates ------------------------------------------------------


def test_detect_duplicates():
    dups = val.detect_duplicates(["a", "b", "a", "c", "a", "b"])
    assert dups == {"a": 3, "b": 2}


def test_detect_duplicates_none():
    assert val.detect_duplicates(["a", "b", "c"]) == {}


def test_detect_duplicates_tuple_keys():
    assert val.detect_duplicates([("X", "Y"), ("X", "Y"), ("Z", "W")]) == {("X", "Y"): 2}


# --- structural -------------------------------------------------------------


def test_issue_is_frozen():
    issue = Issue("f", "range", "msg")
    with pytest.raises(FrozenInstanceError):
        issue.field = "g"  # type: ignore[misc]
