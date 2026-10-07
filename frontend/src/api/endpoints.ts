/**
 * Typed trip API calls.
 *
 * Thin wrappers over the shared Axios client that give the UI a fully-typed contract for
 * the two trip endpoints and turn every failure into a typed {@link ApiError} (the
 * api.md error envelope, a raw HTTP error, or a network failure). The Axios instance is
 * injectable so these are unit-tested without a real server.
 */
import type { AxiosInstance } from "axios";

import apiClient from "@/api/axios";
import { ApiError, toApiError } from "@/api/errors";
import type { TripRequestPayload, TripResponse } from "@/types/trip";

/** `POST /api/generate-trip` — run the pipeline and return the full plan. */
export async function generateTrip(
  payload: TripRequestPayload,
  client: AxiosInstance = apiClient,
): Promise<TripResponse> {
  try {
    const { data } = await client.post<TripResponse>(
      "/api/generate-trip",
      payload,
    );
    return data;
  } catch (err) {
    throw toApiError(err);
  }
}

/** A saved trip's full snapshot (`GET /api/trips/{id}`): the plan as saved + the request. */
export interface SavedTripDetail {
  trip_id: number;
  saved_at: string;
  start_date: string | null;
  end_date: string | null;
  status: "planned" | "reserved";
  transport_reserved: boolean;
  stay_reserved: boolean;
  request: TripRequestPayload | null;
  response: TripResponse;
}

/** `POST /api/trips/{id}/refresh-weather` result. */
export interface WeatherRefreshResult {
  updated: boolean;
  message: string;
  detail: SavedTripDetail;
}

/** `GET /api/trips/{id}` — fetch a saved trip's full snapshot (for re-display + refresh). */
export async function getTrip(
  tripId: number,
  client: AxiosInstance = apiClient,
): Promise<SavedTripDetail> {
  try {
    const { data } = await client.get<SavedTripDetail>(`/api/trips/${tripId}`);
    return data;
  } catch (err) {
    throw toApiError(err);
  }
}

/** `PUT /api/trips/{id}` — overwrite a saved trip with a refreshed plan ("Update saved trip"). */
export async function updateTripSnapshot(
  tripId: number,
  payload: { request: TripRequestPayload; response: TripResponse },
  client: AxiosInstance = apiClient,
): Promise<SavedTripDetail> {
  try {
    const { data } = await client.put<SavedTripDetail>(
      `/api/trips/${tripId}`,
      payload,
    );
    return data;
  } catch (err) {
    throw toApiError(err);
  }
}

/** A saved trip's summary, as returned by `POST /api/trips` / `GET /api/trips`. */
export interface SavedTrip {
  id: number;
  title: string | null;
  notes: string | null;
  origin_label: string | null;
  destination_city: string | null;
  destination_country: string | null;
  transport_mode: string;
  duration_days: number;
  travelers: number;
  budget: number;
  total_cost: number | null;
  overall_score: number | null;
  start_date: string | null;
  end_date: string | null;
  status: "planned" | "reserved";
  transport_reserved: boolean;
  stay_reserved: boolean;
  created_at: string;
}

/** `GET /api/trips` — the signed-in user's saved trips (newest first). */
export async function listTrips(
  client: AxiosInstance = apiClient,
): Promise<SavedTrip[]> {
  try {
    const { data } = await client.get<SavedTrip[]>("/api/trips");
    return data;
  } catch (err) {
    throw toApiError(err);
  }
}

/** `PATCH /api/trips/{id}` — update a saved trip's label/notes or reservation state. */
export async function updateTrip(
  tripId: number,
  payload: {
    title?: string | null;
    notes?: string | null;
    status?: "planned" | "reserved";
    transport_reserved?: boolean;
    stay_reserved?: boolean;
  },
  client: AxiosInstance = apiClient,
): Promise<SavedTrip> {
  try {
    const { data } = await client.patch<SavedTrip>(
      `/api/trips/${tripId}`,
      payload,
    );
    return data;
  } catch (err) {
    throw toApiError(err);
  }
}

/** `POST /api/trips/{id}/refresh-weather` — re-fetch only the forecast for a saved trip. */
export async function refreshTripWeather(
  tripId: number,
  client: AxiosInstance = apiClient,
): Promise<WeatherRefreshResult> {
  try {
    const { data } = await client.post<WeatherRefreshResult>(
      `/api/trips/${tripId}/refresh-weather`,
    );
    return data;
  } catch (err) {
    throw toApiError(err);
  }
}

/** `DELETE /api/trips/{id}` — delete a saved trip. */
export async function deleteTrip(
  tripId: number,
  client: AxiosInstance = apiClient,
): Promise<void> {
  try {
    await client.delete(`/api/trips/${tripId}`);
  } catch (err) {
    throw toApiError(err);
  }
}

/**
 * `POST /api/trips` — save a generated plan to the signed-in user's history.
 *
 * Sends back the exact request + generated response so the stored trip matches what the
 * user saw. Requires a valid access token (attached by the auth layer); a 401 surfaces as
 * an {@link ApiError} the caller can use to redirect to login.
 */
export async function saveTrip(
  payload: { request: TripRequestPayload; response: TripResponse },
  client: AxiosInstance = apiClient,
): Promise<SavedTrip> {
  try {
    const { data } = await client.post<SavedTrip>("/api/trips", payload);
    return data;
  } catch (err) {
    throw toApiError(err);
  }
}

export { ApiError };
