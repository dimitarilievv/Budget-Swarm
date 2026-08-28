# BudgetSwarm — External Source Verification Checklist

This is the **living to-do list** that turns every ⚠️ "requires verification" tag from the technical documentation into a concrete, checkable task. Work through it before relying on any source in code, and update the "Verified on" column as you go.

**Status key:** ✅ confirmed during research (Aug 2026, still re-check) · ⚠️ must verify · ❌ unavailable/rejected

Fill in these columns as you register and test each source:

- **Key obtained?** — did you get an API key / account?
- **Free tier confirmed?** — did you confirm the current free quota yourself?
- **Rate limit noted?** — do you know req/min and req/day/month?
- **ToS checked?** — is your use allowed under their terms?
- **Verified on** — date you last confirmed.

---

## Transport — flights

### Travelpayouts / Aviasales Data API  `[✅ VERIFIED — primary]`  (BS-001)
- [x] Account registered in Travelpayouts affiliate network
- [x] API token obtained (Profile → API token); token only, **no 50k MAU requirement** (that applies to the separate Flight *Search* API, not the Data API)
- [x] Data API returns usable JSON `{success, data, error}` — tested `city-directions`, `prices/calendar`, `search_by_price_range`; all returned data (incl. origin SKP)
- [x] **Rate limits confirmed** (per minute, since 14 Jun 2024): `city-directions` 600 · `prices/calendar` 300 · `prices/cheap` 300 · `prices/direct` 180 · `prices/monthly` 60 · `v3/prices_for_dates` 600 · `v3/search_by_price_range` 600 · `graphql` 60. Exceeding → HTTP 429. Monitor via response headers `X-Rate-Limit`, `X-Rate-Limit-Remaining`, `X-Rate-Limit-Reset`.
- [x] **Cache window confirmed: 2–7 days** → prices are **indicative/cached**, not live bookable. Treat as estimates; provider must cache per city-pair.
- [x] ToS reviewed: cached indicative data for content is fine; automatic collection of booking links is prohibited → do not scrape booking links.
- **Useful endpoints for BudgetSwarm:** `v1/city-directions` (cheapest destinations from an origin — directly supports the budget→destination inversion), `v1/prices/calendar` (cheapest per date), `v3/prices_for_dates` / `v3/search_by_price_range` (route prices; v3 returns more data → fewer calls).
- **DECISION: A — implement as primary flight source.** Cache per city-pair; fallback = distance-based estimate.
- Verified on: 2026-08-19

### Amadeus  `[❌ removed]`
- [x] Confirmed **discontinued** for self-service (17 July 2026)
- Action: none — keep only as a reserved interface slot.

### Aviationstack  `[❌ rejected]`
- [x] Confirmed it is a **status/tracking** API, not fares → not used
- Action: none.

### Kiwi.com / Tequila (incl. via Travelpayouts)  `[❌ rejected]`
- [x] Confirmed invite-only since 2024, no self-service; Kiwi-via-Travelpayouts needs ≥ 50k MAU
- Action: none — do not use `rflights` or old Kiwi tutorials.

### Skyscanner official Travel API  `[❌ rejected]`
- [x] Confirmed partner-gated, application-reviewed, not guaranteed for indie devs
- Action: none — do not block the timeline applying.

### Sky Scrapper (RapidAPI)  `[optional, demo-only]`
- [ ] RapidAPI account created, key obtained
- [ ] ⚠️ Confirm current free tier (research: ~100 req/month)
- [ ] Confirm it returns usable flight data for test routes
- [ ] ToS/reliability risk acknowledged (unofficial scraper) → demo touch only, cache hard
- Verified on: __________

### Fallback: distance-based air-fare estimate  `[fallback]`
- [ ] Decide per-km fare coefficient(s)
- [ ] Document as estimated in output (`quality_flag = estimated`)

---

## Transport — car

### OpenRouteService (ORS)  `[✅ VERIFIED — primary]`  (BS-002)
- [x] Account created (HeiGIT), API key obtained
- [x] **Directions V2 quota confirmed (dashboard): 2000/day, 40/min**
- [x] Also available on the same key (useful): **Geocoding 3000/day (100/min)** for origin→coordinates, Matrix 500/day (40/min), Isochrones 500/day, Snap 2000/day, Elevation 2000/day, POIs 500/day, Optimization 500/day
- [x] Test calls succeeded: Skopje→Thessaloniki, Skopje→Belgrade, Skopje→Sofia
- [x] Response = GeoJSON; `distance` (**metres**) and `duration` (**seconds**) in `features[0].properties.summary` → divide metres by 1000 for the fuel formula
- [x] Coordinate order is **lon,lat**; max driving distance 6000 km per route (sufficient)
- [x] License: **CC-BY 4.0** → attribution to ORS + OpenStreetMap required in app/thesis
- [x] ToS: not for critical navigation (emergency etc.) — not applicable here
- **DECISION: A — implement as primary for car distance/duration.** Fallback = straight-line (haversine) distance estimate. Cache per (origin,destination).
- Verified on: 2026-08-19

### Fuel prices — EU Oil Bulletin / fuel-prices.eu  `[✅ DECIDED — dataset + average]`  (BS-003)
- [x] Authoritative/citable source: **EU Weekly Oil Bulletin** (European Commission, DG ENER) — Euro-95 + diesel, weekly, incl. taxes
- [x] Coverage confirmed: **EU-27 only** → North Macedonia + other non-EU neighbours (RS, BA, ME, AL, TR) are **not** in the core dataset
- [x] Ingestion source: fuel-prices.eu republishes it as **CSV/JSON** (per country, history from 2005); download + cache into `fuel_prices` (weekly bulletin, Thursdays)
- [x] **Strategy:** covered (EU) country → real price from feed (`quality_flag=real`); uncovered (non-EU incl. MK) → **EU-average** price, compute with it (`quality_flag=estimated`); store an `EU_AVG` row at ingestion
- [x] Modelling: car trip uses origin-country price for the whole route → MK origin normally uses the EU-average estimate (honest + flagged)
- [x] Evidence obtained: fuel-prices.eu `llms.txt` snapshot (2026-08-17) with the full EU-27 table — save in repo `data/` folder
- [x] **Confirmed EU average (→ the `EU` fallback row): petrol €1.807/L, diesel €1.952/L** (2026-08-17); per-country range €1.34 (MT) – €2.44 (DK), all sane
- [x] License confirmed: **CC BY 4.0**, attribution "fuel-prices.eu", source EU Commission Oil Bulletin
- [x] Note: real North Macedonia (+ RS/BA/ME/TR) prices ARE available via the separate `/live/` feed (`/live/api.php?action=summary`), but that uses a different methodology (station open-data, 12–18h) than the EC weekly bulletin — so the EU-average decision is kept to stay single-source/citable; `/live/` MK remains an optional future upgrade
- [x] License: open; attribute European Commission Oil Bulletin
- [x] **Refresh method chosen: Option B — automated ingestion script (BS-032)** using fuel-prices.eu machine-readable endpoints (JSON/CSV, e.g. `llms-full.txt`, `/traveler-hud/api.php?country={CC}`, natural-language API). One-time seed acceptable as an interim; the script parses all 27 countries + computes the `EU` average row in one run (no manual weekly entry).
- **DECISION: C (dataset for EU) + D (EU-average for non-EU incl. MK), ingested via Option B (automated script, BS-032).** Cache in `fuel_prices`; app reads DB only, never calls the source live. NOT an AI/LLM at runtime — fuel price feeds the optimizer, so it must stay deterministic.
- **STATUS: ✅ VERIFIED — ready to implement (unblocks BS-032, BS-057).**
- Verified on: 2026-08-19

---

## Transport — bus / train

### FlixBus / FlixTrain — flix-rest (juliuste)  `[❌ RULED OUT — endpoint dead]`  (BS-004)
- [x] Public endpoint `1.flixbus.transport.rest` tested → **unreachable / DNS fails** (2026-08-19)
- [x] Corroborated: underlying `flix` client ~6 years old; sibling repos marked "unmaintained / probably not working"
- [x] License/ToS moot (unofficial; would have needed Flix permission anyway)
- **DECISION: not A.** flix-rest removed from the design.
- **REROUTE:** bus/train (Post-MVP) now uses the **distance-based estimate as the reliable default** (`quality_flag=estimated`); real-fare data is an *optional upgrade* via **Apify FlixBus scraper (BS-005)** or Rome2Rio/Omio — pursue only if time allows. MVP (car) is unaffected.
- Impact: BS-049 / BS-059 rerouted to estimate-first; `flix-rest` provider dropped.
- Verified on: 2026-08-19

### FlixBus / FlixTrain — Apify scraper  `[✅ VERIFIED — optional real-data upgrade]`  (BS-005)
- [x] Apify account created; free plan = **$5 platform credits/month** (resets monthly, no card)
- [x] Actor used: **rl1987/flixbus-api-scraper**; pricing = **$0.01 per trip search** (pay-per-event) + compute (~$0.20/CU) → roughly **~500 searches/month within the free $5** (plenty for pre-caching popular routes). `saswave/flixbus-scraper` is an equivalent alternative.
- [x] Real test passed: **Skopje → Munich** returned 9 trips (origin coverage for NMK confirmed!)
- [x] Fields returned: `priceTotal` / `priceTotalWithFee` / `priceOriginal`, `durationHours`+`durationMinutes`, `transferType` + `legCount` (transfers = legCount−1), `seatsAvailable`, `departure/arrivalStationName`, `operators`, `amenities`, detailed `legs[]`
- [x] **Caveats to handle in code:** (1) some rows have `priceTotal=0` & `seatsAvailable=0` = sold-out/unavailable → **filter these out**; (2) **no currency field in output** → pin currency via input param, treat as EUR, normalize; (3) it's an async actor run (seconds), so **pre-cache popular routes, don't call live per request**
- [x] ToS/reliability: scraper → risk acknowledged; used only for pre-cache; distance-estimate stays as fallback
- **DECISION: A/B — adopt as the Post-MVP real-data source for bus/train (replacing dead flix-rest), pre-cached.** Distance-based estimate remains the default until/unless implemented.
- Verified on: 2026-08-22

### Rome2Rio / Omio wrapper  `[⏸️ DEFERRED — Post-MVP (Decision E)]`  (BS-006)
- [x] Official Rome2Rio/Omio API confirmed **B2B/enterprise-gated** (owned by Omio; licensed to Uber/Google/Kayak) — not self-serve for a student; historical "100k free" (2013) no longer applies
- [x] Data is **indicative fare estimates** anyway (not bookable) — same quality tier as our distance estimate
- [x] Practical access, if ever needed, = another Apify actor (`parseforge/rome2rio-scraper`, multi-modal: flight/train/bus/ferry/car) — same pattern as BS-005
- **DECISION: E — deferred to Post-MVP.** Rationale: (1) **Blocks: None** — nothing depends on it; (2) bus/train is already Post-MVP and already has a real source (Apify FlixBus, BS-005) + fallback (distance estimate); (3) it would only add non-Flix broader coverage via a duplicate scraper. Not worth spike time now.
- Revisit only if broader (non-Flix) coverage becomes a goal; path is documented above.
- Verified on: 2026-08-22

### FlixBus official Developer Portal  `[❌ B2B/gated]`
- [x] Confirmed exists but partner-gated → reserved slot only
- Action: apply only if you specifically want to; don't block on it.

### GTFS fallback  `[fallback, optional]`
- [ ] Decide which corridors (if any) justify GTFS
- [ ] Only pursue if the primary wrapper proves unreliable

### Distance-based estimate  `[fallback, always available]`
- [ ] Decide per-km coefficient per mode (bus vs train)
- [ ] Document as estimated

---

## Accommodation

### Inside Airbnb  `[✅ VERIFIED — primary]`  (BS-007)
- [x] Free download confirmed (no key/account); tested `listings.csv` for **Barcelona** → full rich dataset returned
- [x] License: **CC BY 4.0** (+ CC0 on the data) → academic use with attribution ✅
- [x] Fields confirmed (rich): `price` ($-prefixed), `price_quote_raw` (JSON with **`currency`, `total_price`, `price_per_night`** — currency IS present, e.g. "EUR"), `room_type`, `property_type`, `accommodates`, `bedrooms`, `beds`, `bathrooms`, `latitude`, `longitude`, `amenities`, `minimum_nights`, `availability_365`, `review_scores_rating`, `host_is_superhost`, `license`, `number_of_reviews`
- [x] Snapshot date present (`last_scraped`, e.g. 2026-06); availability is **historical snapshot**, not live — stated honestly
- [ ] Coverage of MY destinations: Barcelona ✅ (tested); check others (Athens/Thessaloniki/Rome/…) against the get-the-data list
- **Caveats for code (BS-033/034/035):** (1) `price` has `$` + commas → strip and cast; prefer `price_quote_raw.price_per_night` + its `currency`; (2) normalize all prices to **EUR**; (3) sentinel `999` appears in min/max nights fields → treat as unknown/null; (4) large files → chunked load
- **DECISION: A/C — primary accommodation dataset.** Covered cities → real (`quality_flag=real`); uncovered cities → estimate from per-city price stats (`quality_flag=estimated`, fallback D).
- Verified on: 2026-08-22

### Makcorps  `[optional live touch]`
- [ ] ⚠️ Confirm current free trial size (research: ~30 calls)
- [ ] Decide whether it's worth wiring in for a live demo only
- Verified on: __________

---

## Attractions / activities

### OpenTripMap  `[✅ VERIFIED — primary]`  (BS-008)
- [x] Key signup works; tested `geoname` (Barcelona → lat/lon/country/population) and `radius` (POIs) — both return data
- [x] Response (GeoJSON/JSON) fields: `name`, `xid`, `point`/coordinates (lat/lon), `kinds` (comma-separated categories, e.g. `historic_architecture,architecture,interesting_places`), `rate` (importance 1–7), `wikidata`
- [x] `kinds` categories feed both preference filtering and the indoor/outdoor mapping (BS-064); `rate` is a ready popularity signal for selecting top attractions
- [x] License: **ODbL** (caching/redistribution allowed with OSM attribution); free tier for non-commercial use
- [ ] Note free-tier daily limit from the dashboard/docs (cache per destination to stay within it)
- **DECISION: A — OpenTripMap primary (rich kinds + rate + wikidata), Overpass fallback.**
- Verified on: 2026-08-22

### OpenStreetMap Overpass  `[✅ VERIFIED — fallback, no key]`
- [x] Confirmed free, no key; tested on overpass-turbo.eu: `node[tourism=attraction]` bbox around Barcelona → **20 nodes** with tags + coordinates
- [x] Returns OSM tags (`tourism`, `historic`, `amenity`, …) usable for categorization + indoor/outdoor
- [x] License: ODbL (OSM attribution)
- [ ] Respect fair-use limits; consider self-hosted Overpass only if heavy
- [ ] Build the OSM-tag → indoor/outdoor/mixed mapping table (BS-064)
- **DECISION: confirmed reliable fallback (guarantees POIs with no key).**
- Verified on: 2026-08-22

### Wikipedia API  `[enrichment, no key]`
- [x] Confirmed free
- [ ] Test summary/image retrieval for a few POIs
- Verified on: __________

---

## Weather

### Open-Meteo  `[✅ VERIFIED — primary, no key]`  (BS-009)
- [x] Confirmed **keyless, no signup, no card**; free for non-commercial use
- [x] **Limits confirmed (official terms): <10,000 calls/day, 5,000/hour, 600/minute** → HTTP 429 on excess (cache per destination+date to stay well within)
- [x] License: **CC BY 4.0** (attribution: link to open-meteo.com); non-commercial explicitly includes **educational/public research** → fine for a thesis
- [x] Forecast fields confirmed: `daily` arrays incl. `temperature_2m_max/min`, `precipitation_sum`, `precipitation_probability_max`, `wind_speed_10m_max`, `weather_code` (WMO); forecast up to 16 days; also a free geocoding endpoint (`geocoding-api.open-meteo.com/v1/search`)
- [x] `weather_conflict` rule (BS-067): flag a day when `precipitation_probability_max` exceeds a configurable threshold (start ~60%)
- **DECISION: A — primary weather source.** Cache with short TTL; graceful degrade if unavailable. (Self-host Docker option exists if ever needed.)
- Verified on: 2026-08-22

---

## Destination data / cost-of-living

### Curated destinations dataset  `[primary, self-built]`
- [x] Assemble city list + coordinates  *(BS-029: `data/curated_destinations.csv`)*
- [x] Populate feature scores (culture/nature/nightlife/food/tourism/season)  *(BS-031: culture/nature/nightlife/food/tourism from OpenStreetMap Overpass POI counts; season from Open-Meteo per-city climate normals; all min–max/[0,1], season varies by month)*
- [x] Populate cost_of_living index  *(BS-031: normalized to [0,1], WhereNext-sourced per BS-011)*
- [x] Document sources for each column (for the thesis)  *(`data/SOURCES.md`, `data/FEATURES_METHODOLOGY.md`)*

### WhereNext  `[✅ RESOLVED — primary cost-of-living source (BS-011)]`
- [x] **Identified: WhereNext = getwherenext.com** — a free, open cost-of-living data platform (a Numbeo alternative)
- [x] Built on **institutional data** (World Bank ICP, OECD, Eurostat, national stats), via a **deterministic** scoring engine (no AI) — aligns with our "no AI in the decision" principle
- [x] Coverage: **95 countries, 380 cities**; **free CC BY 4.0** CSV/JSON downloads + free API, no account/paywall → citable & redistributable (unlike Numbeo's EULA)
- [x] Hands-on (BS-031): verified live API `GET /api/data/cost-of-living` — free, no auth, `{meta, data}` JSON, 95 countries each with `country_code` + `cost_index` (US baseline 82) + sub-indices; all 22 destination countries covered. Ingested via `scripts.ingest_cost_of_living` (cached in `api_cache`), snapshot committed at `data/wherenext_cost_of_living.json`.
- **DECISION: A — WhereNext primary.** Fallback C: Kaggle "Global Cost of Living" dataset (4,500+ cities, static) for cities outside the 380; or a regional average (same pattern as fuel). `cost_of_living` normalized to [0,1] feeds `destination_features` (BS-031) and Scout pre-filter/ranking (BS-054).
- Resolves D-01. Verified on: 2026-08-22

### ~~Numbeo~~  `[rejected — proprietary]`
- [x] Rejected: proprietary EULA, not freely redistributable; estimator/API paywalled ($260/mo estimator; paid API). Unofficial Apify scrapers exist but carry ToS risk. Not suitable when WhereNext gives open, citable data.

---

## LLM (explanation only)  `[✅ DECIDED — Ollama primary (BS-010)]`

**DECISION: A — Ollama (local) primary; Gemini Flash free tier documented alternative; templated fallback always present.** Rationale: reproducible/offline for the defense, no key/quota, MIT, fits the Docker story; the task (narrate fixed numbers) is tiny so a small model suffices. Non-critical — the templated fallback means the system works with no LLM at all. Hands-on install-test happens at implementation (BS-053/077/078).

### Ollama (local)  `[✅ option A — chosen]`
- [x] Confirmed: local, keyless, no rate limits, MIT; REST API at `localhost:11434` (`/api/generate`, `/api/chat`); official `ollama/ollama` Docker image
- [x] Model choice: **llama3.2:3b** (~2 GB, CPU-ok) or gemma3:4b; llama3.2:1b (~1.3 GB) for weak hardware
- [ ] At implementation: `ollama pull llama3.2:3b`, test explanation generation from a sample plan, record latency/RAM
- [x] Model licences (Llama 3.2 / Gemma) allow this use — check model card

### Hosted (Gemini Flash / Groq / other)  `[option B — documented alternative]`
- [x] Fallback path if local compute insufficient; Gemini Flash free tier is the most generous (per BS-006 research)
- [ ] If used: obtain key, store server-side only; confirm it only explains — never touches optimization
- [ ] ⚠️ Don't hard-code free-tier limits (they change)

### Templated fallback  `[always available]`
- [x] Non-LLM template that fills in the numbers → system works with zero LLM; guarantees explanation output

---

## Cross-cutting

- [ ] Every provider wired through its **provider interface** (Section 21), not called directly from agents
- [ ] Every value carries a **`quality_flag`** (real/cached/estimated)
- [ ] **Caching** in place for weather, routes, flight/train prices
- [ ] Secrets only in `.env` (never committed); `.env.example` provided
- [ ] A **"fallback drill"** test exists (disable a provider → still get a valid, flagged plan) for the evaluation chapter
