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

### `PATCH /api/auth/me`
Body `{ "name": "..." | null }` (requires a valid access token). Updates the editable profile fields — currently the display name — and returns the updated user. `401` without a token; `422` if the name exceeds 120 chars.

### `POST /api/auth/change-password`
Body `{ "current_password": "...", "new_password": "..." }` (requires a valid access token). Verifies the current password, stores the new one (min 8 chars), revokes **all** the user's refresh tokens (logging every other session out), and returns a fresh `{ user, tokens }` pair so this client stays signed in. `401` if the current password is wrong or no token; `422` if the new password is invalid or not different.

### `DELETE /api/auth/me`
Permanently deletes the authenticated user and all their data — saved trips (and their activities) and refresh tokens (logging every session out) — then `204`. Irreversible; requires a valid access token (`401` without). Any still-valid access token is useless afterwards.

> `/login`, `/register` and `/change-password` are rate-limited more strictly than other endpoints (default 5/min per IP, `AUTH_RATE_LIMIT_*`).

---

## `POST /api/generate-trip`

The single primary endpoint. Takes user constraints, runs the full agent → optimize → explain pipeline, and returns the chosen plan plus ranked runner-up candidates.

**Public and stateless.** Anyone can generate a plan in full detail (including booking links); it is **not** saved here (`meta.trip_id` is always `null`). To keep a plan, a signed-in user saves it explicitly via `POST /api/trips`. Booking and saving are gated in the UI (a signed-out user is sent to login), not by withholding data here.

### Request body

| Field | Type | Required | Notes |
|-------|------|----------|-------|
| `budget` | number | yes | total budget in EUR, > 0 |
| `duration_days` | integer | yes | trip length, ≥ 1 |
| `travelers` | integer | yes | number of travelers, ≥ 1 |
| `transport_mode` | string | yes | one of `airplane`, `car`, `bus`, `train` |
| `preferences` | object | yes | preference→weight map, e.g. `{"culture":0.9,"food":0.8}` |
| `origin_label` | string | yes | origin city, e.g. `"Skopje"` (geocoded server-side when no coordinates are sent) |
| `origin_lat` | number | no | origin latitude (e.g. from the browser's geolocation); must be sent together with `origin_lon` |
| `origin_lon` | number | no | origin longitude (paired with `origin_lat`) |
| `vehicle_consumption` | number | conditional | required when `transport_mode = "car"` (L/100 km) |
| `fuel_type` | string | no | `petrol` (default) or `diesel`; used only for car-mode fuel costing |

> The request is **date-agnostic**: callers send `duration_days`, not fixed dates. The concrete travel window is derived server-side (from the cheapest/available transport) and returned as `chosen_plan.start_date`/`end_date`. Unknown request fields are rejected (`422`).

```json
{
  "budget": 1000,
  "duration_days": 5,
  "travelers": 2,
  "transport_mode": "car",
  "fuel_type": "petrol",
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
    "start_date": "2026-09-10",
    "end_date": "2026-09-15",
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
      "quality_flag": "estimated",
      "depart_time": null,
      "flight": null
    },
    "accommodation": {
      "name": "Central apartment",
      "accom_type": "entire_home",
      "price_per_night": 60.00,
      "rating": 4.6,
      "latitude": 47.4970,
      "longitude": 19.0510,
      "quality_flag": "real",
      "availability": "snapshot_available",
      "snapshot_date": "2026-09-21",
      "listing_url": "https://www.airbnb.com/rooms/12345",
      "picture_url": "https://…/photo.jpg",
      "review_count": 214,
      "location_score": 4.8,
      "neighbourhood": "District V",
      "amenities": ["Wifi", "Kitchen"],
      "accommodates": 4
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
            "longitude": 19.0396,
            "opening_hours": "Mo-Su 09:00-18:00"
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
    "trip_id": null,
    "generated_at": "2026-08-15T10:22:00Z",
    "degraded_sources": ["transport:estimated"],
    "elapsed_ms": 2140
  }
}
```

### Notes on the response

- `ranked_candidates` powers the **Destination Comparison** page. `feasible: false` means the candidate violated a hard constraint (e.g. over budget) and was excluded from selection — it is shown for transparency, not chosen.
- `quality_flag` (`real` / `cached` / `estimated`) appears on every value that came through a fallback, so the UI can label estimated figures honestly.
- `chosen_plan.start_date`/`end_date` are the **server-derived** travel window (the request carries no dates). They may be absent for an older/legacy response shape.
- `accommodation.availability` is a **snapshot** date-availability verdict (BS-calendar, ADR-017): `snapshot_available` / `snapshot_unavailable` / `availability_unknown` (the default). It reflects the Inside Airbnb **calendar snapshot** taken on `snapshot_date` — it is **not** a live or bookable guarantee, and a missing/uncertain lookup stays `availability_unknown` rather than claiming availability. The 19 estimate-only cities are always `availability_unknown`. The nightly price is still the listing/estimate value (today's snapshots carry no calendar price).
- `accommodation` listing detail (`listing_url`, `picture_url`, `review_count`, `location_score`, `neighbourhood`, `amenities`, `accommodates`) is present only for **real** listings and powers the richer UI card; a price-stats estimate has none of it (`amenities` defaults to `[]`).
- `transport.flight` carries rich per-leg detail (both legs + an Aviasales booking deep link) for **air mode** only; it and `depart_time` are `null` for car/bus/train.
- `meta.trip_id` is **always `null`** here (generate-trip never persists). `meta.saved_at` is set only when serving a stored snapshot via `GET /api/trips/{id}`.
- `meta.degraded_sources` lists any provider that fell back, useful for demos and evaluation (Section 22 & 25 of the technical documentation).
- `explanation` is LLM-generated and may be `null` if the explanation provider is disabled — the plan is still complete without it.

---

## `GET /api/destinations`

Public, read-only listing of the **active** curated destinations — the data source the destination surfaces build on. Query param `limit` (default `50`, range `1–200`). Inactive destinations (soft-disabled) are excluded.

Response `200`:

```json
[
  { "id": 12, "city": "Budapest", "country": "Hungary", "country_code": "HU", "latitude": 47.4979, "longitude": 19.0402 }
]
```

---

## Saved trips (owner-scoped)

All of the following **require authentication** and operate only on the caller's own trips. A trip owned by another user is reported as **`404`** (identical to a missing one) so its existence is never revealed — never `403`.

### `POST /api/trips`
Save a generated plan to the signed-in user's history (the "Save trip" action). Body: `{ "request": <TripRequest>, "response": <TripResponse> }` — the exact request used and the plan received, so the stored trip matches what the user saw. The **full plan is snapshotted** (transport, stay, booking links, weather, dates, ranked candidates). Ownership is taken from the token; a `user_id` in the body is ignored. Returns `201` with the saved trip summary.

### `PUT /api/trips/{trip_id}`
Overwrite a saved trip's snapshot with a refreshed plan (the "Update saved trip" action after **Refresh prices**). Same body as `POST /api/trips`. Keeps the id and original save date. `404` if it isn't the caller's trip. Returns the updated saved-trip detail.

### `GET /api/trips`
List the authenticated user's saved trips (newest first). Each item: `{ id, title, notes, origin_label, destination_city, destination_country, transport_mode, duration_days, travelers, budget, total_cost, overall_score, created_at }`. Powers the **My trips** page.

### `GET /api/trips/{trip_id}`
Return one of the user's own saved trips as a **full snapshot**: `{ trip_id, saved_at, start_date, end_date, status, transport_reserved, stay_reserved, request, response }`, where `response` is the whole stored plan (with `meta.saved_at` set) and `request` is the original inputs (used by "Refresh prices"). Legacy rows saved before snapshots fall back to a reconstruction.
- `200` — the snapshot · `401` — no/invalid token · `404` — unknown id **or** not the caller's trip.

### `PATCH /api/trips/{trip_id}`
Update the user's own trip. Body: `{ "title"?, "notes"?, "transport_reserved"?: bool, "stay_reserved"?: bool, "status"?: "planned"|"reserved" }` — any other field (e.g. `user_id`) is rejected (`422`). **Reservation:** tick `stay_reserved` (and, **for airplane trips**, `transport_reserved`), then lock by setting `status: "reserved"` (rejected `422` until the bookable parts are reserved — the stay always, the flight only when flying; car/bus/train have no flight to book). A reserved trip is locked against price/destination re-planning. Set `status: "planned"` to unlock. Returns the updated trip summary. `404` if it isn't the caller's trip.

### `POST /api/trips/{trip_id}/refresh-weather`
Re-fetch the forecast for the saved trip's destination + dates **and re-plan its days around it**: when the weather-conflict days change, the existing activities are re-scheduled (outdoor stops move off the now-bad days) while the destination/transport/stay stay fixed — useful for a reserved/locked trip. Returns `{ "updated": bool, "message": string, "detail": <SavedTripDetail> }`. Out of the ~16-day forecast horizon, `updated` is `false` and the plan is left unchanged. `404` if it isn't the caller's trip.

### `DELETE /api/trips/{trip_id}`
Delete the user's own trip. `204` on success; `404` if it isn't the caller's trip.

---

## `GET /api/health`

Liveness/readiness check for Docker and monitoring.

```json
{ "status": "ok", "db": "up" }
```

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
| 503 | `SERVICE_UNAVAILABLE` | a backing service (e.g. the destination datastore) is temporarily unavailable — transient, worth retrying |
| 500 | `INTERNAL_ERROR` | unexpected server error |

> Validation happens **twice**: in the browser for UX, and again server-side (authoritative). The server never trusts client-side validation.

---

## Rate limiting & CORS

- **Rate limiting** is applied per client to `/api/generate-trip` to protect the backend's own upstream free tiers (Section 24), and a stricter, separate limit guards `/api/auth/login` and `/api/auth/register` (default 5/min per IP).
- **CORS** allows only the configured frontend origin(s); no wildcard in production.
- No external API keys are ever sent to or exposed in the frontend.
