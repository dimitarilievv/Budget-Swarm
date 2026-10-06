# BudgetSwarm — API Reference

The backend (FastAPI) exposes a small, stable HTTP contract. All decision logic lives server-side; the frontend only sends constraints and renders structured results. FastAPI auto-generates interactive OpenAPI docs at `/docs` — this file is the human-readable summary and the source of truth for the request/response shapes.

Base URL (dev): `http://localhost:8000`

All requests and responses are `application/json`.

---

## Authentication

Authentication uses **JWT access tokens** (short-lived, ~15 min) plus **refresh tokens** (~7 days). A plan can be **generated anonymously**, but saving a trip to history and viewing its booking details require being signed in. Send the access token on protected requests via the header:

```
Authorization: Bearer <access_token>
```

Refresh tokens are opaque strings stored **hashed** server-side and **rotated** on every refresh (the presented token is single-use); logout revokes a refresh token. Secrets and lifetimes are read from environment variables (`JWT_SECRET`, `ACCESS_TOKEN_EXPIRE_MINUTES`, `REFRESH_TOKEN_EXPIRE_DAYS`).

### `POST /api/auth/register`
Create an account. Validates email and password (**min 8 chars**); rejects a duplicate email with `409`.

Request: `{ "email": "a@b.com", "password": "password123", "name": "Ann" }` (`name` optional).
Response `201`: `{ "user": { "id", "email", "name", "created_at", "updated_at" }, "tokens": { "access_token", "refresh_token", "token_type": "bearer", "expires_in" } }`. The password hash is **never** returned.

### `POST /api/auth/login`
Verify credentials, return the same `{ user, tokens }` body. On failure returns a single generic `401` **"Invalid email or password"** for both an unknown email and a wrong password (no user enumeration).

### `POST /api/auth/refresh`
Body `{ "refresh_token": "..." }` → `200` with a **new** token pair; the old refresh token is revoked (rotation). An invalid/expired/revoked token → `401`.

### `POST /api/auth/logout`
Body `{ "refresh_token": "..." }` → `204`. Revokes the refresh token; idempotent.

### `GET /api/auth/me`
Returns the authenticated user (`401` without a valid token).

> `/login` and `/register` are rate-limited more strictly than other endpoints (default 5/min per IP, `AUTH_RATE_LIMIT_*`).

---

## `POST /api/generate-trip`

The single primary endpoint. Takes user constraints, runs the full agent → optimize → explain pipeline, and returns the chosen plan plus ranked runner-up candidates.

**Auth is optional here.** Without a token the plan is returned but **not saved** (`meta.trip_id` is `null`) and its **booking links are withheld** (accommodation `listing_url`, flight `booking_url`). With a valid `Authorization: Bearer` token the plan is saved to the user's history (its `trip_id` is returned) and booking links are included. Ownership always comes from the token — a `user_id` in the body is rejected.

### Request body

| Field | Type | Required | Notes |
|-------|------|----------|-------|
| `budget` | number | yes | total budget in EUR, > 0 |
| `duration_days` | integer | yes | trip length, ≥ 1 |
| `travelers` | integer | yes | number of travelers, ≥ 1 |
| `transport_mode` | string | yes | one of `airplane`, `car`, `bus`, `train` |
| `preferences` | object | yes | preference→weight map, e.g. `{"culture":0.9,"food":0.8}` |
| `origin_label` | string | yes | origin city, e.g. `"Skopje"` |
| `vehicle_consumption` | number | conditional | required when `transport_mode = "car"` (L/100 km) |

```json
{
  "budget": 1000,
  "duration_days": 5,
  "travelers": 2,
  "transport_mode": "car",
  "preferences": { "culture": 0.9, "food": 0.8, "sightseeing": 0.7 },
  "origin_label": "Skopje",
  "vehicle_consumption": 6.5
}
```

### Response body (200)

```json
{
  "chosen_plan": {
    "destination": {
      "id": 12,
      "city": "Budapest",
      "country": "Hungary",
      "latitude": 47.4979,
      "longitude": 19.0402
    },
    "overall_score": 0.8734,
    "total_cost": 812.50,
    "cost_breakdown": {
      "transport": 151.00,
      "accommodation": 240.00,
      "activities": 421.50
    },
    "transport": {
      "mode": "car",
      "cost": 151.00,
      "duration_min": 780,
      "distance_km": 1300,
      "quality_flag": "estimated"
    },
    "accommodation": {
      "name": "Central apartment",
      "accom_type": "entire_home",
      "price_per_night": 60.00,
      "rating": 4.6,
      "latitude": 47.4970,
      "longitude": 19.0510,
      "quality_flag": "real"
    },
    "itinerary": [
      {
        "day_number": 1,
        "activities": [
          {
            "name": "Buda Castle",
            "category": "historic",
            "indoor_outdoor": "outdoor",
            "start_time": "10:00",
            "end_time": "12:30",
            "was_rescheduled": false,
            "latitude": 47.496,
            "longitude": 19.0396
          }
        ]
      }
    ],
    "weather": [
      {
        "forecast_date": "2026-09-10",
        "temp_min": 14.0,
        "temp_max": 23.0,
        "precip_prob": 70.0,
        "conditions": "rain",
        "weather_conflict": true
      }
    ]
  },
  "ranked_candidates": [
    {
      "destination": { "city": "Budapest", "country": "Hungary" },
      "total_cost": 812.50,
      "overall_score": 0.8734,
      "transport_cost": 151.00,
      "accommodation_cost": 240.00,
      "experience_score": 0.88,
      "weather_score": 0.75,
      "preference_score": 0.92,
      "feasible": true
    },
    {
      "destination": { "city": "Prague", "country": "Czechia" },
      "total_cost": 1010.00,
      "overall_score": 0.81,
      "transport_cost": 221.00,
      "accommodation_cost": 280.00,
      "experience_score": 0.82,
      "weather_score": 0.85,
      "preference_score": 0.85,
      "feasible": false
    }
  ],
  "explanation": "Budapest was selected because it stays within your €1000 budget, best matches your culture and food preferences, and its rainy day 3 is handled by moving outdoor sightseeing to day 4.",
  "meta": {
    "trip_id": 341,
    "generated_at": "2026-08-15T10:22:00Z",
    "degraded_sources": ["transport:estimated"],
    "elapsed_ms": 2140
  }
}
```

### Notes on the response

- `ranked_candidates` powers the **Destination Comparison** page. `feasible: false` means the candidate violated a hard constraint (e.g. over budget) and was excluded from selection — it is shown for transparency, not chosen.
- `quality_flag` (`real` / `cached` / `estimated`) appears on every value that came through a fallback, so the UI can label estimated figures honestly.
- `meta.degraded_sources` lists any provider that fell back, useful for demos and evaluation (Section 22 & 25 of the technical documentation).
- `explanation` is LLM-generated and may be `null` if the explanation provider is disabled — the plan is still complete without it.

---

## Saved trips (owner-scoped)

All of the following **require authentication** and operate only on the caller's own trips. A trip owned by another user is reported as **`404`** (identical to a missing one) so its existence is never revealed — never `403`.

### `GET /api/trips`
List the authenticated user's saved trips (newest first). Each item: `{ id, title, notes, destination_city, destination_country, transport_mode, duration_days, travelers, total_cost, overall_score, created_at }`.

### `GET /api/trips/{trip_id}`
Return one of the user's own stored plans, in the same `chosen_plan` shape as generate-trip.
- `200` — the stored plan · `401` — no/invalid token · `404` — unknown id **or** not the caller's trip.

### `PATCH /api/trips/{trip_id}`
Update the editable labels of the user's own trip. Body: `{ "title"?: string, "notes"?: string }` — any other field (e.g. `user_id`) is rejected (`422`). Returns the updated trip summary. `404` if it isn't the caller's trip.

### `DELETE /api/trips/{trip_id}`
Delete the user's own trip. `204` on success; `404` if it isn't the caller's trip.

---

## `GET /api/health`

Liveness/readiness check for Docker and monitoring.

```json
{ "status": "ok", "db": "up" }
```

---

## Admin — candidate destinations (BS-029)

Grow the curated candidate list by city name. Backing sources: Open-Meteo
geocoding (no key). Feature enrichment (cost / POI / season / scores) is a
separate batch step, so a freshly added city returns `enrichment_pending: true`.

### `GET /api/admin/destinations/geocode`

Ranked geocoding matches for a city name, for a picker to choose from.

| Query | Type | Required | Notes |
|-------|------|----------|-------|
| `name` | string | yes | city name to look up |
| `count` | integer | no | max results, 1–10 (default 5) |

```json
[
  {
    "city": "Barcelona", "country": "Spain", "country_code": "ES",
    "latitude": 41.3888, "longitude": 2.159,
    "population": 1686208, "admin1": "Catalonia",
    "label": "Barcelona, Spain (Catalonia)"
  }
]
```

`502` if the geocoding provider is unavailable.

### `POST /api/admin/destinations`

Add a destination, either by `name` (geocoded server-side) or from an explicit
candidate. Deduped on `(city, country)`.

```json
// by name
{ "name": "Barcelona" }
// or an explicit candidate (e.g. one picked from /geocode)
{ "city": "Barcelona", "country": "Spain", "country_code": "ES",
  "latitude": 41.3888, "longitude": 2.159 }
```

Response — `201` when newly created, `200` when it already existed:

```json
{
  "created": true,
  "destination": {
    "id": 31, "city": "Barcelona", "country": "Spain",
    "country_code": "ES", "latitude": 41.3888, "longitude": 2.159
  },
  "enrichment_pending": true
}
```

`404` if no city matches the name; `422` if neither `name` nor a full explicit
candidate is supplied.

---

## Error format

All errors use a consistent envelope.

```json
{
  "error": {
    "code": "VALIDATION_ERROR",
    "message": "budget must be greater than 0",
    "field": "budget"
  }
}
```

| HTTP | `code` | When |
|------|--------|------|
| 422 | `VALIDATION_ERROR` | request failed schema/range validation |
| 401 | `UNAUTHORIZED` | missing, invalid, or expired access token; failed login |
| 404 | `NOT_FOUND` | trip id does not exist (or isn't the caller's) |
| 409 | `CONFLICT` | resource already exists (e.g. duplicate email on register) |
| 429 | `RATE_LIMITED` | client exceeded the backend's rate limit |
| 503 | `NO_PLAN_AVAILABLE` | no feasible plan could be produced even after fallbacks (rare) |
| 500 | `INTERNAL_ERROR` | unexpected server error |

> Validation happens **twice**: in the browser for UX, and again server-side (authoritative). The server never trusts client-side validation.

---

## Rate limiting & CORS

- **Rate limiting** is applied per client to `/api/generate-trip` to protect the backend's own upstream free tiers (Section 24), and a stricter, separate limit guards `/api/auth/login` and `/api/auth/register` (default 5/min per IP).
- **CORS** allows only the configured frontend origin(s); no wildcard in production.
- No external API keys are ever sent to or exposed in the frontend.
