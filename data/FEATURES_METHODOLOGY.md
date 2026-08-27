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

`data/destination_features_raw.csv` holds one row per curated destination
(the same `(city, country)` keys as `data/curated_destinations.csv`, BS-029)
with the following **raw, un-normalized** columns:

| Column       | Meaning                                             | Raw scale |
|--------------|-----------------------------------------------------|-----------|
| `cost_index` | Cost-of-living index (higher = more expensive)      | ~30–100   |
| `culture`    | Density/renown of cultural assets (museums, heritage)| 0–100    |
| `nature`     | Access to nature (coast, mountains, parks)          | 0–100     |
| `nightlife`  | Nightlife intensity (bars, clubs, scene)            | 0–100     |
| `food`       | Food-scene strength                                 | 0–100     |
| `tourism`    | Tourism intensity (visitor volume/renown)           | 0–100     |
| `climate`    | Köppen-style climate class for the season model     | category  |

### Sources of the raw signal

- **`cost_index`** — cost-of-living, resolved to **WhereNext**
  (getwherenext.com) as the primary source per **ADR/BS-011**: a free, open
  (CC BY 4.0) platform built on institutional data (World Bank ICP, OECD,
  Eurostat) via a deterministic scoring engine. For cities outside WhereNext's
  380-city coverage the BS-011 decision falls back to the Kaggle "Global Cost
  of Living" dataset or a regional average. The indices here are **curated
  indicative baselines** on that WhereNext/Kaggle scale (Western-European
  capital ≈ 100), pending live WhereNext ingestion; they are honest estimates
  in the same spirit as the flagged fuel/flight estimates (ADR-005/ADR-009),
  reproducible and refreshable.
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

Because these raw indices are curated, the *engineering contribution* this
ticket delivers is the **normalization and season model** below, which is fully
deterministic and re-runs unchanged when the raw layer is later swapped for live
ingestion.

## 2. Normalization to [0,1]

Every published score is normalized with **min–max normalization across the
destination set**, the method documented for the scoring model (technical
documentation §14.2):

```
normalized(x) = (x − min_over_destinations) / (max_over_destinations − min_over_destinations)
```

- Applied independently to `culture`, `nature`, `nightlife`, `food`, `tourism`
  and `cost_index`.
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

- The raw culture/nature/nightlife/food/tourism indices and the `cost_index` are
  **curated indicative baselines**, not yet a live ingestion. They are
  reproducible and documented, and the normalization/season engineering is
  final; swapping the raw layer for live OSM/WhereNext data changes the inputs,
  not the method.
- The season curves use climate-class archetypes, not per-city climate normals.
- Min–max normalization is sensitive to outliers by construction; with a
  curated, bounded set this is acceptable and keeps the [0,1] guarantee exact.
