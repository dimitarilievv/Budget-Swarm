"""Optional POI enrichment — Wikipedia summaries (BS-065; technical-doc §11).

Enrichment is a **Post-MVP, nice-to-have** step: it attaches a short human-readable
summary to an activity so the UI can show context, but the plan is fully usable
without it. It therefore sits behind the small :class:`ActivityEnricher` protocol
and is entirely optional — ExperienceAgent runs it only when an enricher is
configured and enrichment is requested, and a failure degrades to *no summary*
rather than an error (ADR-005, "degrade, don't crash").

:class:`WikipediaEnricher` is the concrete enricher: it looks a POI up on Wikipedia
(by title, using the free REST summary endpoint — no key) and returns the plain
``extract``, trimmed to a sentence-ish length. As with the other adapters (BS-047..051)
the HTTP call is injected as a :data:`SummaryTransport` so the enricher is unit
tested against a recorded response with no network. Wikidata ids are carried through
from the source but the REST summary endpoint is title-based, so the POI name is the
lookup key; a Wikidata-based lookup can be added behind the same protocol later.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Protocol, runtime_checkable

logger = logging.getLogger(__name__)

WIKI_SUMMARY_URL = "https://en.wikipedia.org/api/rest_v1/page/summary/"

# Trim summaries so a long lead paragraph does not bloat the payload.
DEFAULT_MAX_CHARS = 600


@dataclass(frozen=True)
class SummaryRequest:
    """A lookup for one POI's summary (title is the key; wikidata carried through)."""

    name: str
    wikidata: str | None = None


# The injected HTTP boundary: given a request, return the summary text or None.
SummaryTransport = Callable[[SummaryRequest], str | None]


@runtime_checkable
class ActivityEnricher(Protocol):
    """A source of an optional short summary for a POI (kept source-agnostic)."""

    def summary_for(self, name: str, wikidata: str | None = None) -> str | None: ...


class WikipediaEnricher:
    """Enrich a POI with its Wikipedia summary (BS-065).

    Never raises: any transport failure or missing page yields ``None`` so
    enrichment stays strictly optional. The returned text is trimmed to
    ``max_chars`` at a word boundary.
    """

    def __init__(self, transport: SummaryTransport, *, max_chars: int = DEFAULT_MAX_CHARS) -> None:
        self._transport = transport
        self.max_chars = max_chars

    def summary_for(self, name: str, wikidata: str | None = None) -> str | None:
        if not name or not name.strip():
            return None
        try:
            text = self._transport(SummaryRequest(name.strip(), wikidata))
        except Exception as exc:  # noqa: BLE001 - enrichment is optional; degrade to None
            logger.info("enrichment: Wikipedia lookup failed for %r (%s)", name, exc)
            return None
        return _trim(text, self.max_chars)


def _trim(text: str | None, max_chars: int) -> str | None:
    """Trim ``text`` to ``max_chars`` at a word boundary; None/blank -> None."""
    if not text or not text.strip():
        return None
    clean = text.strip()
    if len(clean) <= max_chars:
        return clean
    cut = clean[:max_chars].rsplit(" ", 1)[0].rstrip()
    return f"{cut}…"


def wikipedia_http_transport(
    *,
    base_url: str = WIKI_SUMMARY_URL,
    timeout: float = 10.0,
    lang: str = "en",
    client: Any | None = None,
) -> SummaryTransport:
    """Build a :data:`SummaryTransport` for the Wikipedia REST summary endpoint.

    Needs no key. A 404 (no page) or any transport error yields ``None`` so
    enrichment degrades silently. Returns the page ``extract`` text.
    """
    import urllib.parse

    import httpx

    url_base = base_url.replace("//en.", f"//{lang}.") if lang != "en" else base_url

    def transport(request: SummaryRequest) -> str | None:
        owns = client is None
        http = client or httpx.Client(timeout=timeout)
        try:
            title = urllib.parse.quote(request.name.replace(" ", "_"), safe="")
            resp = http.get(f"{url_base}{title}", headers={"Accept": "application/json"})
            if resp.status_code == 404:
                return None
            resp.raise_for_status()
            body = resp.json()
            extract = body.get("extract") if isinstance(body, dict) else None
            return extract if isinstance(extract, str) else None
        except httpx.HTTPError as exc:
            logger.info("enrichment: Wikipedia HTTP error for %r (%s)", request.name, exc)
            return None
        finally:
            if owns:
                http.close()

    return transport
