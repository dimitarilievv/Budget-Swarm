# BudgetSwarm — Limitations, Data-Quality & Terms-of-Service Disclosure

This is the honest limitations chapter for the thesis (the full form of technical-documentation **§28**). Acknowledging limits — and disclosing exactly how firm each number is and where the data comes from — is part of the project's academic defensibility and its core design posture: **always produce a plan, and always be transparent about the firmness of its numbers.**

Two cross-cutting ideas make every limitation below *manageable rather than fatal*, and are referenced throughout as the mitigations:

- **`quality_flag` (`real` / `cached` / `estimated`)** — every value a provider returns carries a provenance flag that propagates to the optimizer (as a small, capped scoring penalty — ADR-005/ADR-013) and to the API response (`meta.degraded_sources`, and the UI's "estimated" labels). Nothing is presented as firmer than it is.
- **The layered fallback chain (ADR-004 / ADR-005, §21–22)** — every external source sits behind a provider interface with `primary → fallback → local dataset/estimate`. A missing or failed piece *degrades* a candidate (flagged) instead of aborting the request, so the system never depends on any single third party being up.

The decision itself is a deterministic OR-Tools optimization (ADR-001); the limitations below concern the **data fed into it** and the **presentation around it**, not the correctness of the optimization, which is reproducible and budget-provable for a given input.

---

## 1. Data-quality & source disclosure (at a glance)

| Source | Role | Provenance | Key limitation | Mitigation |
|--------|------|-----------|----------------|------------|
| Travelpayouts / Aviasales | flight prices | indicative, **cached 2–7 days** (not live/bookable) | prices are estimates; a fare may differ at booking; booking-link scraping is prohibited by ToS | `quality_flag`; distance-fare fallback; we link out (Aviasales deep link), never scrape booking links |
| OpenRouteService | car distance/duration | real (free tier) | 2000/day, 40/min quota; CC-BY attribution | caching per route; haversine distance fallback |
| Fuel prices (EU Oil Bulletin via fuel-prices.eu) | car fuel cost | real for covered countries; **national averages** | not the exact station price; some countries via a different (station-avg) methodology; AL falls back to the EU average | `quality_flag=estimated` where modeled; `source`/`price_date` recorded per row |
| Inside Airbnb — listings | accommodation (11/30 cities) | real **snapshot** (e.g. 2026-09) | representative, not a live/bookable price; snapshot can be weeks/months old | `quality_flag=real` but point-in-time; snapshot date surfaced |
| Inside Airbnb — calendar | date-availability (ADR-017) | real **snapshot** | **not live/bookable**; stale between snapshots; current snapshots carry no nightly price | status `snapshot_available`/`snapshot_unavailable`/`availability_unknown` + snapshot date; price falls back to the flagged estimate |
| WhereNext cost-of-living model | accommodation estimate (19/30 cities) | **modeled estimate** (ADR-016) | country-level proxy, not a market price; leave-one-out MAPE ≈ 19% (±~20% band) | `quality_flag=estimated`, `source='wherenext-cost-model'`; ADR-015 prefers real data |
| FlixBus (Apify), Sky Scrapper, Rome2Rio/Omio | bus/train & optional flight (post-MVP) | **unofficial scrapers/wrappers** | outside the target sites' ToS; fragile; indicative only | **optional, pre-cached, non-commercial, not redistributed**; distance estimate is the always-available default |
| OpenTripMap / Overpass (OSM) | POIs/attractions | real (free/keyless) | coverage varies by city; sparse opening hours; ODbL attribution | Overpass fallback (no key); unfiltered-itinerary safety net |
| Open-Meteo | weather | real (keyless) | forecast only ~16 days out; probabilistic | climatology fallback beyond the horizon; weather-unchecked is flagged |
| Ollama / hosted LLM | explanation only | generated text | non-deterministic wording; may be unavailable | templated fallback; **never touches the decision** (ADR-001) |

---

## 2. Prices are indicative, not bookable

Flight fares (Travelpayouts/Aviasales) are **cached, indicative** prices (a 2–7 day cache window), not live bookable quotes; bus/train fares — where available at all — are indicative or estimated; accommodation nightly prices are **representative** snapshot figures. BudgetSwarm **plans and links out** (e.g. an Aviasales round-trip deep link) but **never books and never charges** — prices and availability must be reconfirmed on the provider's site at checkout, where they may differ.

*Mitigation:* every price carries `quality_flag`; estimated/degraded prices surface in `meta.degraded_sources` and the UI; the hard-budget constraint is enforced on the numbers shown, and the optimizer gently down-ranks estimate-heavy candidates (ADR-005/013).

## 3. Accommodation: snapshot listings and snapshot date-availability (not live)

Inside Airbnb covers **11 of the 30** curated destinations with **real snapshot** listings; the snapshot (e.g. 2026-09) can be weeks or months old. Per-date **availability** comes from the Inside Airbnb `calendar.csv` snapshot (ADR-017) and is reported as one of `snapshot_available` / `snapshot_unavailable` / `availability_unknown`, **always with the snapshot date** — it is **snapshot data, never a live or bookable guarantee**: a listing may since have been booked, delisted, or re-priced. Availability can be stale between snapshots (Inside Airbnb publishes roughly monthly/quarterly), and **all current (2026-09) snapshots omit a nightly `price` column**, so a date-specific total is never invented — the nightly price falls back to the flagged listing/estimate value.

*Mitigation:* the default status is `availability_unknown`, so a missing/uncertain lookup is never turned into a false positive; the snapshot date is always carried; the refresh is bounded, idempotent, per-city isolated, and failure-safe (a bad download keeps the previous snapshot — ADR-017).

## 4. Estimated accommodation for the 19 uncovered cities

The 19 destinations Inside Airbnb does not cover use a **modeled** nightly price from the WhereNext cost-of-living model (ADR-016), calibrated on the covered cities' real medians. This is a **country-level proxy, not a market price**, with a leave-one-out MAPE of roughly **19%** (a coarse ±~20% band). No listing names, photos, or URLs are ever fabricated — only aggregate price statistics (`source='wherenext-cost-model'`, `sample_size=0`).

*Mitigation:* flagged `estimated` end-to-end; the **source-priority policy (ADR-015)** prefers any viable real-data destination over an estimated one, so a cheap estimate can never beat a genuine real-data option.

## 5. Estimated fuel costs

Car-mode fuel cost is computed as `(distance/100) × consumption × price_per_litre` using **national-average** fuel prices (EU Weekly Oil Bulletin, ingested via fuel-prices.eu), not the exact price at the station the traveller uses. EU-27 countries use the bulletin; several non-EU neighbours (MK, RS, BA, ME, TR) use station-averaged `/live/` prices (a different methodology, labelled as such); Albania falls back to the EU-average row.

*Mitigation:* a modeled figure by design, flagged `estimated`; each row records its `source` and `price_date` so the methodology stays transparent and citable (ADR-009).

## 6. Transport coverage

There is **no free, official, pan-European bus/train fare API**. Those fares are therefore indicative or estimated, and bus/train is a post-MVP mode; the always-available default is a **distance-based estimate** (flagged `estimated`). Flight-price access is scarce and indicative (Section 9.1 / ADR-002, ADR-011). Air-mode date-matching only confirms a window when a **real** round-trip fare exists on the exact dates; otherwise it degrades to an estimated window, flagged.

*Mitigation:* the distance estimate guarantees a number for every route; real fares upgrade it when available; `quality_flag` distinguishes the two.

## 7. Unofficial / scraper sources and Terms of Service

Several **optional, post-MVP** transport sources are unofficial scrapers or community wrappers that technically fall **outside the target sites' Terms of Service**: the **FlixBus Apify scraper** (BS-005), the **Sky Scrapper (RapidAPI)** flight wrapper, and **Rome2Rio/Omio** wrappers (BS-006). For a **non-commercial, small-scale, academic prototype** this is a common and generally low-practical-risk approach, and BudgetSwarm treats it responsibly and discloses it explicitly:

- These sources are **optional** — the MVP (car mode) does not use any of them, and the **distance-based estimate is always retained as the fallback**, so the system never *depends* on a scraper being up.
- Where used, data is **aggressively cached per city-pair** to minimise requests, **not redistributed**, and **kept non-commercial**.
- The **Travelpayouts ToS prohibits automated collection of booking links** — so BudgetSwarm **never scrapes booking links**; it builds its own Aviasales *search* deep link from IATA codes + dates instead.
- Official/partner-gated APIs that a student cannot obtain (Amadeus self-service — discontinued July 2026; Kiwi/Tequila; Skyscanner; FlixBus Developer Portal) are **rejected and recorded with reasons** (ADR-002, ADR-003, ADR-011), not worked around.

*Mitigation:* unofficial sources are isolated behind the provider abstraction, cached, flagged, and never load-bearing; the mandatory fallback chain (ADR-005) means a dead or blocked scraper only costs precision, never a plan.

## 8. Free-tier limits

Several sources have small free quotas (Travelpayouts per-endpoint per-minute limits; OpenRouteService 2000/day, 40/min; Open-Meteo <10k/day; Apify ~$5/month credits; Makcorps ~30 calls). Sustained or spiky load could exhaust them and trigger HTTP 429.

*Mitigation:* aggressive caching (`api_cache` + TTL, Section 23) keeps requests off the hot path; per-client rate limiting on `/api/generate-trip` protects the backend's upstream quotas; every provider has a keyless/offline fallback so a throttled source degrades rather than fails.

## 9. Weather uncertainty

Forecasts are **probabilistic** and less reliable further out; Open-Meteo only forecasts ~16 days ahead. Weather-aware replanning (moving outdoor activities off high-rain-probability days) is **best-effort, not guaranteed-correct**, and a far-future trip gets climatology (typical conditions) or is weather-unchecked.

*Mitigation:* beyond the forecast horizon the plan degrades to climatology/weather-unchecked (flagged), never a fabricated forecast; the conflict rule threshold is configurable.

## 10. External API availability and response drift

External providers can change their schema, rate limits, or disappear entirely — the **Amadeus self-service shutdown mid-project** is the proof. Adapters absorb field-shape changes, but breakage is still possible.

*Mitigation:* the provider abstraction (ADR-004) localises each vendor to one adapter, and the fallback chain keeps the system producing a (flagged) plan even when a provider breaks — swapping or dropping a provider is one adapter, not an architecture change.

## 11. Incomplete datasets and coverage

The curated destination set (30 cities), Inside Airbnb coverage (11/30), and POI coverage are **not exhaustive**; coverage is bounded by the datasets. Cost-of-living is a **country-level** WhereNext index, so a capital can differ from its country average.

*Mitigation:* curated, reproducible datasets (ADR-007) with documented sources (`data/SOURCES.md`); gaps are filled by flagged estimates, and a destination with *neither* real nor estimated data is honestly excluded (`missing_accommodation`) rather than given an invented price.

## 12. The LLM explains, it does not decide

The natural-language explanation is **LLM-generated** (Ollama locally, or a hosted provider) and its wording is **non-deterministic**; the provider may be unavailable, in which case a **templated** explanation is used, or the plan is returned with none. The LLM **never participates in the decision** — the destination, budget check, and itinerary are all produced by the deterministic optimizer (ADR-001), so explanation variance never changes the chosen plan.

*Mitigation:* templated fallback guarantees output; the decision/explanation separation is the project's central correctness claim.

## 13. Source-priority trade-off

The source-priority policy (ADR-015) restricts selection to real-accommodation candidates whenever at least one is viable. This is the intended, honest trade-off, but it can occasionally **set aside a genuinely better-value *estimated* city** in favour of a real-data one.

*Mitigation:* the policy is deterministic and togglable; estimated cities remain *eligible* (a real fallback tier, not excluded); the trade-off is documented in ADR-015.

## 14. Scope boundaries (not defects)

- **No live booking or payment** — BudgetSwarm plans and links out; it never reserves inventory or charges. "Reserve" in a saved trip is a user-side bookkeeping lock, not a real booking.
- **No machine learning in the MVP** (ADR-010) — methods are constraint optimization, multi-agent data integration, and deterministic algorithms; learned preferences are future work.
- **Authentication is in scope but minimal** (ADR-012) — passwords are bcrypt-hashed (never plaintext or returned), refresh tokens are hashed/rotated/revocable, `JWT_SECRET` is env-only and fails fast if missing; it is an MVP auth surface, not a hardened IdP. No secrets are committed (`.env` only).

---

## How the design reduces these risks (summary)

Provider abstraction (ADR-004) + layered fallbacks (ADR-005) + curated/local datasets (ADR-007) + caching (Section 23) + explicit `quality_flag` provenance (surfaced as `meta.degraded_sources` and UI "estimated" labels) + a deterministic, budget-provable optimizer (ADR-001). The system is built to **always produce a plan** and to be **transparent about the firmness of its numbers** — the realistic, honest posture for a project built on free, third-party data.

See also: technical-documentation §28 (summary), `docs/adr.md` (ADR-002/005/009/011/015/016/017), `docs/verification-checklist.md` (per-source ToS/free-tier verification), and `data/SOURCES.md` (dataset provenance).
