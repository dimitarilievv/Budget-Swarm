/**
 * Wire types for the trip API (mirrors backend/schemas/trip_schema.py).
 * Kept minimal — the input form (a later ticket) fills these in.
 */
import type { BrowserOrigin } from "./geolocation";

export type TransportMode = "airplane" | "car" | "bus" | "train";

export interface TripRequestPayload {
  budget: number;
  duration_days: number;
  travelers: number;
  transport_mode: TransportMode;
  preferences: Record<string, number>;
  origin_label: string;
  /** Optional origin coordinates — supplied together, e.g. from the browser's geolocation. */
  origin_lat?: number | null;
  origin_lon?: number | null;
  vehicle_consumption?: number | null;
}

/**
 * Map a resolved {@link BrowserOrigin} to the request's origin fields, so a form can
 * spread them straight into the payload:
 *   `{ ...form, ...originFields(origin) }`
 */
export function originFields(
  origin: BrowserOrigin,
): Pick<TripRequestPayload, "origin_label" | "origin_lat" | "origin_lon"> {
  return {
    origin_label: origin.originLabel,
    origin_lat: origin.originLat,
    origin_lon: origin.originLon,
  };
}
