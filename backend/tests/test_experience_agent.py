"""Unit tests for ExperienceAgent — POI retrieval + category filter (BS-064).

No network: OpenTripMap/Overpass are fed recorded responses via injected
transports, and the per-destination cache uses a FakeStore. Covers preference-
category filtering, bounded/clamped counts, per-destination caching, domain
normalization (no leaked provider fields), and degrade-to-empty.
"""

from __future__ import annotations

import pytest
from db.retrieval import CacheEntry, read_through

from ai_engine.agents.experience_agent import (
    CANDIDATE_POOL,
    DEFAULT_LIMIT,
    MAX_LIMIT,
    ExperienceAgent,
)
from ai_engine.domain import Activity, IndoorOutdoor, QualityFlag
from ai_engine.providers.attraction import (
    AttractionService,
    OpenTripMapProvider,
    OverpassProvider,
)
from ai_engine.utils.api_clients import RetryPolicy, read_through_hook
from ai_engine.utils.geo import GeoPoint

BARCELONA = GeoPoint(lat=41.3874, lon=2.1686)

# Recorded OpenTripMap radius JSON (ratings drive ordering; unnamed dropped).
RECORDED_POIS = [
    {
        "xid": "N1",
        "name": "Sagrada Familia",
        "rate": 7,
        "kinds": "religion,architecture,interesting_places",
        "wikidata": "Q48435",
        "point": {"lon": 2.1744, "lat": 41.4036},
    },
    {
        "xid": "N2",
        "name": "Picasso Museum",
        "rate": 6,
        "kinds": "museums,cultural",
        "point": {"lon": 2.18, "lat": 41.385},
    },
    {
        "xid": "N3",
        "name": "Park Güell",
        "rate": 5,
        "kinds": "gardens_and_parks,urban_environment",
        "point": {"lon": 2.1527, "lat": 41.4145},
    },
    {
        "xid": "N4",
        "name": "El Xampanyet",
        "rate": 4,
        "kinds": "bars,foods",
        "point": {"lon": 2.18, "lat": 41.38},
    },
]


def _instant_retry() -> RetryPolicy:
    return RetryPolicy(sleep=lambda _d: None, jitter=lambda: 0.0)


class FakeStore:
    def __init__(self):
        self.data = {}
        self.puts = 0

    def get(self, provider, signature):
        return self.data.get((provider, signature))

    def put(self, provider, signature, body, expires_at):
        self.puts += 1
        self.data[(provider, signature)] = CacheEntry(body, None, expires_at)


def _service(otm_transport, *, cache=None, overpass_transport=None) -> AttractionService:
    primary = OpenTripMapProvider(otm_transport, retry=_instant_retry(), cache=cache)
    fallback = OverpassProvider(
        overpass_transport or (lambda _req: {"elements": []}), retry=_instant_retry()
    )
    return AttractionService(primary=primary, fallback=fallback)


def _agent(otm_transport=None, **kw) -> ExperienceAgent:
    transport = otm_transport or (lambda _req: RECORDED_POIS)
    return ExperienceAgent(_service(transport), **kw)


# --- category filtering -----------------------------------------------------


def test_returns_pois_matching_preference_categories():
    agent = _agent()
    museums = agent.activities_for(BARCELONA, destination="Barcelona", categories=["museum"])
    assert [a.name for a in museums] == ["Picasso Museum"]


def test_category_filter_is_case_insensitive_substring():
    agent = _agent()
    parks = agent.activities_for(BARCELONA, categories=["PARK", "garden"])
    assert [a.name for a in parks] == ["Park Güell"]


def test_multiple_categories_union():
    agent = _agent()
    picks = agent.activities_for(BARCELONA, categories=["bar", "food"])
    assert [a.name for a in picks] == ["El Xampanyet"]


def test_no_categories_keeps_all_ordered_by_popularity():
    agent = _agent()
    names = [a.name for a in agent.activities_for(BARCELONA)]
    # OpenTripMap orders by rating desc; nothing filtered out.
    assert names == ["Sagrada Familia", "Picasso Museum", "Park Güell", "El Xampanyet"]


def test_no_match_returns_empty_without_error():
    agent = _agent()
    assert agent.activities_for(BARCELONA, categories=["spa"]) == []


# --- normalization ----------------------------------------------------------


def test_outputs_domain_activities_without_provider_fields():
    agent = _agent()
    activity = agent.activities_for(BARCELONA, categories=["museum"])[0]
    assert isinstance(activity, Activity)
    assert activity.indoor_outdoor is IndoorOutdoor.INDOOR  # museums -> indoor
    assert activity.quality_flag is QualityFlag.REAL
    for leaked in ("provider", "popularity", "source_id", "wikidata", "categories"):
        assert not hasattr(activity, leaked)


def test_outdoor_classification_maps_to_enum():
    agent = _agent()
    park = agent.activities_for(BARCELONA, categories=["garden"])[0]
    assert park.indoor_outdoor is IndoorOutdoor.OUTDOOR


# --- bounded count ----------------------------------------------------------


def test_default_limit_bounds_count():
    many = [
        {
            "xid": f"N{i}",
            "name": f"Museum {i}",
            "rate": 5,
            "kinds": "museums",
            "point": {"lon": 2.1, "lat": 41.3},
        }
        for i in range(30)
    ]
    agent = _agent(lambda _req: many)
    assert len(agent.activities_for(BARCELONA, categories=["museum"])) == DEFAULT_LIMIT


def test_explicit_limit_respected_and_clamped():
    many = [
        {
            "xid": f"N{i}",
            "name": f"Museum {i}",
            "rate": 5,
            "kinds": "museums",
            "point": {"lon": 2.1, "lat": 41.3},
        }
        for i in range(30)
    ]
    agent = _agent(lambda _req: many)
    assert len(agent.activities_for(BARCELONA, limit=3)) == 3
    assert len(agent.activities_for(BARCELONA, limit=99)) == MAX_LIMIT


def test_agent_requests_a_stable_candidate_pool():
    # The provider is asked for the constant pool (not the caller's limit) so the
    # per-destination cache key stays stable.
    seen = []

    def transport(req):
        seen.append(req.limit)
        return RECORDED_POIS

    agent = _agent(transport)
    agent.activities_for(BARCELONA, limit=3)
    agent.activities_for(BARCELONA, limit=10)
    assert seen == [CANDIDATE_POOL, CANDIDATE_POOL]


# --- caching per destination ------------------------------------------------


def test_cached_per_destination_on_repeat():
    store = FakeStore()
    hook = read_through_hook(read_through, store)
    agent = ExperienceAgent(_service(lambda _req: RECORDED_POIS, cache=hook))

    first = agent.activities_for(BARCELONA, destination="Barcelona", categories=["museum"])
    second = agent.activities_for(BARCELONA, destination="Barcelona", categories=["museum"])
    assert first[0].quality_flag is QualityFlag.REAL
    assert second[0].quality_flag is QualityFlag.CACHED
    assert [a.name for a in first] == [a.name for a in second]
    assert store.puts == 1  # only the miss hit the "network"


# --- degrade, don't crash ---------------------------------------------------


def test_no_pois_from_any_source_returns_empty():
    # OpenTripMap empty and Overpass empty -> AllProvidersFailed -> [].
    agent = ExperienceAgent(
        _service(lambda _req: [], overpass_transport=lambda _req: {"elements": []})
    )
    assert agent.activities_for(BARCELONA, destination="Nowhere") == []


# --- construction validation ------------------------------------------------


def test_invalid_construction_rejected():
    service = _service(lambda _req: RECORDED_POIS)
    with pytest.raises(ValueError, match="default_limit"):
        ExperienceAgent(service, default_limit=0)
    with pytest.raises(ValueError, match="candidate_pool"):
        ExperienceAgent(service, max_limit=20, candidate_pool=5)


# --- normalization: est_duration + class (BS-065) ---------------------------


def test_activities_get_estimated_duration_and_class():
    agent = _agent()
    by_name = {a.name: a for a in agent.activities_for(BARCELONA)}
    museum = by_name["Picasso Museum"]
    park = by_name["Park Güell"]
    assert museum.est_duration_minutes == 120  # museum heuristic
    assert museum.indoor_outdoor is IndoorOutdoor.INDOOR
    assert park.est_duration_minutes == 60  # "garden" heuristic
    assert park.indoor_outdoor is IndoorOutdoor.OUTDOOR
    # Every activity gets a duration and a class.
    assert all(a.est_duration_minutes is not None for a in by_name.values())


# --- optional Wikipedia enrichment (BS-065) ---------------------------------


class _FakeEnricher:
    def __init__(self, summaries=None, fail=False):
        self.summaries = summaries or {}
        self.fail = fail
        self.calls = 0

    def summary_for(self, name, wikidata=None):
        self.calls += 1
        if self.fail:
            return None
        return self.summaries.get(name)


def test_enrichment_off_by_default():
    enricher = _FakeEnricher(summaries={"Picasso Museum": "A museum."})
    agent = ExperienceAgent(_service(lambda _req: RECORDED_POIS), enricher=enricher)
    activities = agent.activities_for(BARCELONA)  # enrich defaults to False
    assert all(a.description is None for a in activities)
    assert enricher.calls == 0


def test_enrichment_attaches_summary_when_requested():
    enricher = _FakeEnricher(summaries={"Picasso Museum": "A modern-art museum."})
    agent = ExperienceAgent(_service(lambda _req: RECORDED_POIS), enricher=enricher)
    museum = next(
        a
        for a in agent.activities_for(BARCELONA, categories=["museum"], enrich=True)
        if a.name == "Picasso Museum"
    )
    assert museum.description == "A modern-art museum."


def test_enrichment_requested_without_enricher_is_noop():
    agent = _agent()  # no enricher configured
    activities = agent.activities_for(BARCELONA, enrich=True)
    assert all(a.description is None for a in activities)


def test_enrichment_failure_leaves_description_none():
    agent = ExperienceAgent(_service(lambda _req: RECORDED_POIS), enricher=_FakeEnricher(fail=True))
    activities = agent.activities_for(BARCELONA, enrich=True)
    assert activities and all(a.description is None for a in activities)
