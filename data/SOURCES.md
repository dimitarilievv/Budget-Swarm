# Curated destinations dataset — source notes (BS-029)

**Artifact:** `data/curated_destinations.csv`
**Assembled:** 2026-08-26
**Rows:** 30 European cities
**Schema:** `city,country,country_code,lat,lon`

- `country_code` — ISO 3166-1 alpha-2 (uppercase). This is the join key to
  `fuel_prices.country_code` (see `docs/schema.sql`) and to per-country data.
- `lat` / `lon` — decimal degrees (WGS84), city-centre reference point. These
  map to `destinations.latitude` / `destinations.longitude` on ingest.

## Purpose

This is the **raw ingestion source** for ScoutAgent's curated PostgreSQL
destination dataset (technical documentation §8, §19). It is deliberately a
*controlled domain dataset*, not mock data: a normalized table of real
destinations assembled from open sources. No scoring or feature engineering is
done here — that happens downstream (BS-030 loads these rows into the
`destinations` table; `destination_features` is populated separately, enriched
by WhereNext cost-of-living data per §8.1).

## Selection criteria

Target: 20–40 European cities that are **realistic budget destinations from a
Central/Eastern-European origin** (the thesis's worked examples — Budapest,
Prague, Belgrade, Thessaloniki — imply a Balkan/CEE home origin such as Sofia).
The list therefore leans toward Central Europe, the Balkans, the Baltics, and
affordable Southern-European cities, with a few pricier anchors (Vienna,
Lisbon) kept for contrast so Scout's budget pre-filter has range to work with.

## Sources

Every row is drawn from open, redistributable references:

1. **City / country names & ISO 3166-1 alpha-2 codes** —
   [ISO 3166 country codes](https://www.iso.org/obp/ui/#search) (the alpha-2
   standard; also mirrored in the public-domain
   [`datasets/country-codes`](https://github.com/datasets/country-codes) list).
2. **Coordinates (lat/lon)** — city-centre points from
   [GeoNames](https://www.geonames.org/) (CC BY 4.0), cross-checked against
   [OpenStreetMap / Nominatim](https://www.openstreetmap.org/) (ODbL). Rounded
   to 4 decimal places (~11 m precision), which is ample for coarse
   transport-distance estimates.

Both coordinate sources are open-licence and citable for the thesis; no
scraped, proprietary, or EULA-restricted data (e.g. Numbeo) is used here.

## Notes / caveats

- `Czechia` is used as the country name (ISO short name) rather than the older
  "Czech Republic".
- Istanbul (`TR`) straddles Europe/Asia; included as a realistic budget
  destination reachable overland from the origin. Its centre point is on the
  European side.
- Coordinates are single city-centre reference points, not bounding boxes;
  per-POI and per-accommodation coordinates come from the specialist agents
  downstream, not from this file.

## Related datasets (BS-031 feature engineering)

- `data/destination_features_raw.csv` — per-destination factor counts
  (culture/nature/nightlife/food/tourism) + climate class. The five factor
  columns are **generated from OpenStreetMap** by `scripts.ingest_osm_features`,
  not hand-authored.
- `data/osm_features_raw.json` — **committed snapshot of OpenStreetMap POI
  counts** per destination (Overpass API, 12 km radius). **Attribution (required
  by ODbL):** "© OpenStreetMap contributors, licensed under the ODbL 1.0." No API
  key required.
- `data/wherenext_cost_of_living.json` — **committed snapshot of WhereNext
  cost-of-living data** (getwherenext.com, `GET /api/data/cost-of-living`), used
  as the offline/CI fallback for cost-of-living ingestion (BS-011). **Attribution
  (required by CC BY 4.0):** "Cost-of-living data © WhereNext (getwherenext.com),
  licensed under CC BY 4.0." The snapshot's `meta` block records the source,
  licence, and version. Full methodology: `data/FEATURES_METHODOLOGY.md`.

## Reproduce / validate

Row count and required columns are validated by
`backend/tests/test_curated_destinations.py` (runs in the fast unit-test path,
no database required):

```bash
cd backend && python -m pytest tests/test_curated_destinations.py -v
```
