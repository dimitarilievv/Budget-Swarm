"""Unit tests for the curated indoor/outdoor classifier (BS-066).

Known tags/kinds map to their expected class; ambiguous or conflicting tokens
resolve to mixed; and a definite provider hint is used only when the table cannot
decide. This is the interface to weather replanning, so the table's behaviour is
pinned here.
"""

from __future__ import annotations

import pytest

from ai_engine.agents.indoor_outdoor import (
    classify_activity,
    classify_categories,
    coerce,
)
from ai_engine.domain import IndoorOutdoor

IN = IndoorOutdoor.INDOOR
OUT = IndoorOutdoor.OUTDOOR
MIX = IndoorOutdoor.MIXED


# --- known category tokens -> expected class --------------------------------


@pytest.mark.parametrize(
    ("tokens", "expected"),
    [
        (["museums"], IN),
        (["galleries"], IN),
        (["theatres_and_entertainments"], IN),
        (["cinema"], IN),
        (["malls"], IN),
        (["aquarium"], IN),
        (["library"], IN),
        (["gardens_and_parks", "urban_environment"], OUT),
        (["natural"], OUT),
        (["beaches"], OUT),
        (["viewpoint"], OUT),
        (["national_parks"], OUT),
        (["nature_reserve"], OUT),
        (["historic"], OUT),
        (["castle"], OUT),
        (["monument"], OUT),
    ],
)
def test_known_tokens_classify_as_expected(tokens, expected):
    assert classify_categories(tokens) is expected


# --- ambiguous / conflicting -> mixed ---------------------------------------


@pytest.mark.parametrize(
    "tokens",
    [
        [],
        ["attraction"],
        ["interesting_places"],
        ["urban_environment"],
        [""],
        ["religion", "architecture"],
    ],
)
def test_ambiguous_tokens_default_to_mixed(tokens):
    assert classify_categories(tokens) is MIX


def test_conflicting_indoor_and_outdoor_is_mixed():
    # A castle with an indoor museum matches both tables -> genuinely mixed.
    assert classify_categories(["castle", "museum"]) is MIX


def test_matching_is_case_insensitive_substring():
    assert classify_categories(["MUSEUMS"]) is IN
    assert classify_categories(["Gardens_And_Parks"]) is OUT


# --- classify_activity: table first, then hint, then mixed ------------------


def test_table_wins_over_hint():
    # Definite table answer is used even if the provider hint disagrees.
    assert classify_activity(["museums"], hint="outdoor") is IN


def test_definite_hint_used_when_table_ambiguous():
    # "attraction" is ambiguous; the provider's OSM-tag hint decides.
    assert classify_activity(["attraction"], hint="outdoor") is OUT
    assert classify_activity(["attraction"], hint="indoor") is IN


def test_ambiguous_table_and_hint_defaults_to_mixed():
    assert classify_activity(["attraction"], hint="mixed") is MIX
    assert classify_activity(["attraction"], hint=None) is MIX
    assert classify_activity([], hint="garbage") is MIX


def test_every_activity_gets_a_class():
    # No combination of inputs ever returns something outside the enum.
    for tokens in ([], ["museum"], ["park"], ["attraction"], ["castle", "museum"]):
        assert isinstance(classify_activity(tokens), IndoorOutdoor)


# --- coerce -----------------------------------------------------------------


def test_coerce_accepts_enum_string_and_junk():
    assert coerce(IN) is IN
    assert coerce("outdoor") is OUT
    assert coerce("nonsense") is MIX
    assert coerce(None) is MIX
