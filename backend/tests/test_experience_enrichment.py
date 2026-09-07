"""Unit tests for the est-duration heuristic and Wikipedia enricher (BS-065).

No network: the enricher's HTTP boundary is injected. Covers the category→duration
table, the default, summary trimming, and degrade-to-None on failure.
"""

from __future__ import annotations

import pytest

from ai_engine.agents.duration import (
    DEFAULT_DURATION_MINUTES,
    estimate_duration_minutes,
)
from ai_engine.providers.enrichment import SummaryRequest, WikipediaEnricher

# --- duration heuristic -----------------------------------------------------


@pytest.mark.parametrize(
    ("tokens", "expected"),
    [
        (["museums"], 120),
        (["galleries"], 90),
        (["gardens_and_parks"], 60),  # "garden" matches before "park"
        (["natural"], 90),
        (["viewpoint"], 30),
        (["castle"], 75),
        (["beaches"], 120),
        (["zoo"], 150),
    ],
)
def test_duration_heuristic_known_categories(tokens, expected):
    assert estimate_duration_minutes(tokens) == expected


def test_duration_defaults_when_unmatched():
    assert estimate_duration_minutes(["attraction"]) == DEFAULT_DURATION_MINUTES
    assert estimate_duration_minutes([]) == DEFAULT_DURATION_MINUTES


def test_duration_is_case_insensitive():
    assert estimate_duration_minutes(["MUSEUMS"]) == 120


# --- Wikipedia enricher -----------------------------------------------------


def test_enricher_returns_trimmed_summary():
    enricher = WikipediaEnricher(lambda _req: "A famous basilica in Barcelona.")
    assert enricher.summary_for("Sagrada Familia") == "A famous basilica in Barcelona."


def test_enricher_passes_name_and_wikidata_to_transport():
    seen = []

    def transport(req: SummaryRequest):
        seen.append((req.name, req.wikidata))
        return "text"

    WikipediaEnricher(transport).summary_for("Park Güell", wikidata="Q123")
    assert seen == [("Park Güell", "Q123")]


def test_enricher_trims_to_word_boundary():
    long_text = "word " * 300  # 1500 chars
    out = WikipediaEnricher(lambda _req: long_text, max_chars=50).summary_for("X")
    assert out is not None and len(out) <= 51 and out.endswith("…")


def test_enricher_degrades_to_none_on_failure():
    def boom(_req):
        raise RuntimeError("network down")

    assert WikipediaEnricher(boom).summary_for("X") is None


def test_enricher_none_for_blank_name_or_empty_result():
    assert WikipediaEnricher(lambda _req: "text").summary_for("  ") is None
    assert WikipediaEnricher(lambda _req: None).summary_for("X") is None
    assert WikipediaEnricher(lambda _req: "   ").summary_for("X") is None
