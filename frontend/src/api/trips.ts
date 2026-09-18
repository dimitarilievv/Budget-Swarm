/**
 * Typed trip API calls.
 *
 * Thin wrappers over the shared Axios client that give the UI a fully-typed contract for
 * the two trip endpoints and turn every failure into a typed {@link ApiError} (the
 * api.md error envelope, a raw HTTP error, or a network failure). The Axios instance is
 * injectable so these are unit-tested without a real server.
 */
import type { AxiosInstance } from "axios";

import apiClient from "./client";
import { ApiError, toApiError } from "./errors";
import type { ChosenPlan, TripRequestPayload, TripResponse } from "./types";

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

/** `GET /api/trips/{id}` — fetch a previously generated plan by id. */
export async function getTrip(
  tripId: number,
  client: AxiosInstance = apiClient,
): Promise<ChosenPlan> {
  try {
    const { data } = await client.get<ChosenPlan>(`/api/trips/${tripId}`);
    return data;
  } catch (err) {
    throw toApiError(err);
  }
}

export { ApiError };
