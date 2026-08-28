# Destination feature engineering — methodology (BS-031)

This document explains how the normalized `destination_features` rows that drive
ScoutAgent ranking are produced. It is the thesis-facing record of *what each
score means*, *where the raw signal comes from*, and *how it is normalized*. The
computation itself lives in `backend/scripts/compute_destination_features.py`
and is covered by `backend/tests/test_compute_destination_features.py`.

The feature store is a **controlled domain dataset**, not mock data (ADR-007):
every column is a real, defensible feature assembled from open sources and
normalized deterministically, exactly as a production recommender precomputes a
feature store. Same input → same scores.

## 1. Inputs

Cost-of-living is **not** a column in this CSV — it is ingested live from
WhereNext (see below). The CSV holds one row per curated destination (the same
`(city, country)` keys as `data/curated_destinations.csv`, BS-029) with the
following **raw, un-normalized** columns:

| Column         | Meaning                                              | Raw scale |
|----------------|------------------------------------------------------|-----------|
| `country_code` | ISO 3166-1 alpha-2 — the join key to WhereNext cost  | code      |
| `culture`      | Density/renown of cultural assets (museums, heritage)| 0–100     |
| `nature`       | Access to nature (coast, mountains, parks)           | 0–100     |
| `nightlife`    | Nightlife intensity (bars, clubs, scene)             | 0–100     |
| `food`         | Food-scene strength                                  | 0–100     |
| `tourism`      | Tourism intensity (visitor volume/renown)            | 0–100     |
| `climate`      | Köppen-style climate class for the season model      | category  |

### Sources of the raw signal

- **cost-of-living** — ingested **live from WhereNext** (getwherenext.com), the
  primary source decided in **BS-011**: a free, no-auth, CC BY 4.0 open-data
  platform built on institutional data (World Bank ICP 2021, OECD, Eurostat) via
  a deterministic scoring engine. `scripts.wherenext` calls the endpoint
  `GET /api/data/cost-of-living` (95 countries, each with a numeric `cost_index`,
  US baseline = 82, higher = more expensive) and each destination takes its
  **country's** index, joined on `country_code`. Following ADR-008 ("cache into
  Postgres rather than call live") and ADR-005 (mandatory fallback), the raw
  payload is cached in the `api_cache` table by `scripts.ingest_cost_of_living`,
  and a committed snapshot (`data/wherenext_cost_of_living.json`, CC BY 4.0, with
  WhereNext's `meta`/attribution block) is the always-available offline/CI floor.
  Resolution order is cache → live → snapshot. WhereNext also exposes city-level
  and sub-index (grocery/rent/utilities/transport) data; the MVP uses the
  country-level composite `cost_index`, with the Kaggle "Global Cost of Living"
  dataset / regional average as the BS-011 fallback for any uncovered country.
- **`culture` / `nature` / `nightlife` / `food` / `tourism`** — curated
  indicative baselines derived from open POI/tourism signals: OpenStreetMap /
  OpenTripMap POI-category density (museums & heritage → culture; parks, coast
  and mountains → nature; bars & clubs → nightlife; restaurants → food) and
  public tourism statistics (Eurostat / national tourist boards) for `tourism`.
  These are the same open sources the ScoutAgent enrichment pipeline draws on
  (technical documentation §8, §19). They are baselines to be replaced by a
  live OSM Overpass / OpenTripMap ingestion; documenting them as curated is the
  academically honest framing (ADR-007).
- **`climate`** — the **Köppen–Geiger** climate classification (an open index),
  collapsed to five travel-relevant classes: `mediterranean`, `continental`,
  `oceanic`, `alpine`, `baltic`.

Cost-of-living is therefore real ingested data; the five subjective factor
indices remain curated baselines. The *engineering contribution* this ticket
delivers is the **live WhereNext ingestion + caching** and the **normalization
and season model** below, which are fully deterministic and re-run unchanged when
the subjective factor layer is later swapped for a live OSM/OpenTripMap feed.

## 2. Normalization to [0,1]

Every published score is normalized with **min–max normalization across the
destination set**, the method documented for the scoring model (technical
documentation §14.2):

```
normalized(x) = (x − min_over_destinations) / (max_over_destinations − min_over_destinations)
```

- Applied independently to `culture`, `nature`, `nightlife`, `food`, `tourism`
  and the WhereNext cost index. The cost index is normalized across only the
  destinations WhereNext actually covers, so an uncovered country (which gets a
  NULL `cost_of_living`) never distorts the [0,1] range for the rest.
- If a column is constant across the set (`max == min`), every value maps to
  **0.5** (a neutral score) rather than dividing by zero.
- Results are stored quantized to 3 decimal places to match the schema
  (`NUMERIC(4,3)` for the factor scores, `NUMERIC(6,3)` for `cost_of_living`).

**`cost_of_living` orientation.** It is normalized so **0 = cheapest** in the
set and **1 = most expensive**. It is stored as a plain normalized index; the
Scout ranking layer (BS-054) is responsible for *inverting* it into a
cost penalty (lower cost is better), exactly as §14.2 prescribes for cost-like
factors. Keeping the feature store orientation-neutral avoids baking a ranking
policy into the data.

**Global vs. per-request normalization.** The feature store normalizes across
the *full* destination set once (a stable baseline). The Scout ranking layer may
re-normalize across the *candidate set* per request (§14.2); the two are
complementary — the store gives comparable baselines, the request layer sharpens
them for the short list actually under consideration.

## 3. Season model (`season_score`)

`season_score` answers "how good is this destination during the travel window?"
and therefore **must vary by month**. Each climate class carries a 12-month
suitability curve in [0,1], reflecting typical travel comfort (temperature and
precipitation) for that class:

| Climate        | Peak months        | Character                                   |
|----------------|--------------------|---------------------------------------------|
| `mediterranean`| May–Sep (mild winter)| warm, long season, pleasant shoulder months|
| `oceanic`      | Jun–Sep (mild all year)| temperate, gentle seasonal swing          |
| `continental`  | May–Sep            | cold winters, warm summers                  |
| `alpine`       | Jun–Sep            | cool, short strong summer                    |
| `baltic`       | Jun–Aug            | cold, short northern summer                  |

For a travel window spanning one or more months, `season_score` is the **mean of
the monthly suitabilities over the months in the window**. A single-month window
(the default, using the current month) reduces to that month's value. This makes
the score depend on the requested dates: a Mediterranean coast city scores high
in July and low in January, while the difference is even sharper for a Baltic
city.

The curves are a documented project judgment (🔧), derived from Köppen climate
character rather than a live climate feed; they are deliberately simple and
inspectable, and can be replaced by an Open-Meteo climate-normals ingestion
without changing the interface.

## 4. Output

The script writes one `destination_features` row per destination it can match to
a loaded `destinations` row (join on `(city, country)`), with:

`cost_of_living`, `culture_score`, `nature_score`, `nightlife_score`,
`food_score`, `tourism_score`, `season_score` — all in **[0,1]**.

The upsert is keyed on `destination_id` (the table's primary key), so re-running
is idempotent: existing rows are refreshed in place (including a bumped
`updated_at`), never duplicated. Destinations present in the raw CSV but not yet
loaded into the database are logged and skipped; the run does not abort.

## 5. Limitations (stated for honesty)

- **Cost-of-living is real WhereNext data**, but at **country** granularity — all
  destinations in a country share their country's `cost_index`. WhereNext's
  city-level dataset and sub-indices are available for a future refinement.
- The five subjective factor indices (culture/nature/nightlife/food/tourism)
  remain **curated indicative baselines** derived from open POI/tourism signals,
  not yet a live OSM/OpenTripMap ingestion. They are reproducible and documented,
  and the normalization/season engineering is final; swapping that layer for live
  data changes those inputs, not the method.
- The season curves use climate-class archetypes, not per-city climate normals.
- Min–max normalization is sensitive to outliers by construction; with a
  curated, bounded set this is acceptable and keeps the [0,1] guarantee exact.

## 6. Adding another cost-of-living source

Cost-of-living sits behind a small provider interface (`scripts/cost_of_living.py`,
`CostOfLivingProvider`) so sources are swappable and chainable, per ADR-004. Each
source only has to return a `country_code → cost_index` mapping; normalization and
everything downstream are source-agnostic. To add one:

1. Write a class with a `name` and a `load()` returning a `ProviderResult`
   (mapping, provenance label, meta). A ready-made `CsvCostOfLivingProvider`
   already covers any `country_code,cost_index` CSV — e.g. a normalized export of
   the Kaggle "Global Cost of Living" dataset, the BS-011 fallback.
2. Pass it into `resolve_cost_index([primary, fallback, …])`. Providers are tried
   in priority order (first to supply a country wins); a provider that raises is
   logged and skipped (ADR-005, degrade-don't-crash).
3. Any country still missing is filled with a **regional average** of the
   already-resolved countries in the same region (`EUROPEAN_SUBREGIONS`), the
   BS-011 floor. Provenance is tracked per country (`wherenext:live`,
   `csv:kaggle`, `regional-average:balkans`, …).

The `compute_destination_features` CLI exposes this via `--cost-fallback-csv`,
which appends a `CsvCostOfLivingProvider` after WhereNext.
