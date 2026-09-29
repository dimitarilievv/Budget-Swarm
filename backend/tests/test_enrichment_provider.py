"""Unit tests for the WikipediaEnricher POI-summary adapter (BS-065; Phase 11).

No network: the enricher is fed via an injected :data:`SummaryTransport`, and the
production HTTP transport is checked against ``httpx.MockTransport`` (keyless). Covers
the recorded-response happy path, summary trimming/normalization, and the "degrade to
no summary" path that keeps enrichment strictly optional (ADR-005).
"""

from __future__ import annotations

import httpx
import pytest

from ai_engine.providers.enrichment import (
    DEFAULT_MAX_CHARS,
    SummaryRequest,
    WikipediaEnricher,
    _trim,
    wikipedia_http_transport,
)

EXTRACT = (
    "Buda Castle is the historical castle and palace complex of the Hungarian kings in Budapest."
)


# --- enricher: recorded response + normalization ----------------------------


def test_returns_trimmed_summary_from_transport():
    enricher = WikipediaEnricher(lambda req: EXTRACT)
    assert enricher.summary_for("Buda Castle") == EXTRACT


def test_passes_name_and_wikidata_through_to_the_transport():
    seen: dict[str, SummaryRequest] = {}

    def transport(req: SummaryRequest) -> str:
        seen["req"] = req
        return "ok"

    WikipediaEnricher(transport).summary_for("  Buda Castle  ", wikidata="Q191052")
    assert seen["req"] == SummaryRequest("Buda Castle", "Q191052")  # name is stripped


def test_long_summary_is_trimmed_at_a_word_boundary():
    long_text = "word " * 400  # ~2000 chars, well over the default
    out = WikipediaEnricher(lambda req: long_text, max_chars=50).summary_for("X")
    assert out is not None
    assert len(out) <= 51 and out.endswith("…")
    assert "  " not in out.rstrip("…")  # cut at a boundary, no dangling partial word


def test_short_summary_is_returned_as_is():
    enricher = WikipediaEnricher(lambda req: "  Short.  ", max_chars=DEFAULT_MAX_CHARS)
    assert enricher.summary_for("X") == "Short."


# --- enricher: degrade to no summary ----------------------------------------


@pytest.mark.parametrize("blank", ["", "   ", None])
def test_blank_name_skips_lookup_and_returns_none(blank):
    calls = []
    enricher = WikipediaEnricher(lambda req: calls.append(req) or "x")
    assert enricher.summary_for(blank) is None
    assert calls == []  # the transport is never called for a blank name


@pytest.mark.parametrize("empty", [None, "", "   "])
def test_empty_transport_result_is_none(empty):
    assert WikipediaEnricher(lambda req: empty).summary_for("Buda Castle") is None


def test_transport_error_degrades_to_none():
    def boom(req: SummaryRequest) -> str:
        raise RuntimeError("wiki down")

    assert WikipediaEnricher(boom).summary_for("Buda Castle") is None


# --- _trim helper -----------------------------------------------------------


def test_trim_none_and_blank_are_none():
    assert _trim(None, 100) is None
    assert _trim("   ", 100) is None


def test_trim_exact_length_is_unchanged():
    text = "x" * 10
    assert _trim(text, 10) == text


# --- production HTTP transport (keyless, recorded) ---------------------------


def test_http_transport_returns_extract_and_is_keyless():
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["request"] = request
        return httpx.Response(200, json={"extract": EXTRACT, "title": "Buda Castle"})

    client = httpx.Client(transport=httpx.MockTransport(handler))
    transport = wikipedia_http_transport(client=client)

    assert transport(SummaryRequest("Buda Castle")) == EXTRACT
    req = captured["request"]
    assert "Buda_Castle" in str(req.url)  # spaces -> underscores in the title
    assert "Authorization" not in req.headers and "apikey" not in req.url.params  # keyless


def test_http_transport_404_returns_none():
    client = httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(404)))
    transport = wikipedia_http_transport(client=client)
    assert transport(SummaryRequest("Nonexistent Place")) is None


def test_http_transport_5xx_degrades_to_none():
    client = httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(503)))
    transport = wikipedia_http_transport(client=client)
    assert transport(SummaryRequest("Buda Castle")) is None


def test_http_transport_missing_extract_is_none():
    client = httpx.Client(
        transport=httpx.MockTransport(lambda r: httpx.Response(200, json={"title": "X"}))
    )
    transport = wikipedia_http_transport(client=client)
    assert transport(SummaryRequest("X")) is None


def test_http_transport_lang_switches_host():
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["host"] = request.url.host
        return httpx.Response(200, json={"extract": "Hallo"})

    client = httpx.Client(transport=httpx.MockTransport(handler))
    transport = wikipedia_http_transport(client=client, lang="de")
    assert transport(SummaryRequest("Berlin")) == "Hallo"
    assert captured["host"] == "de.wikipedia.org"
