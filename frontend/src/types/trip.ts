/**
 * Wire types for the trip API — a hand-written mirror of docs/api.md
 * (and backend/schemas/trip_schema.py). These are the single source of type-truth the
 * UI builds against, so a response that drifts from the contract is a compile error.
 */
import type { BrowserOrigin } from "@/api/geolocation";

export type TransportMode = "airplane" | "car" | "bus" | "train";
export type FuelType = "petrol" | "diesel";
export type QualityFlag = "real" | "cached" | "estimated";
export type IndoorOutdoor = "indoor" | "outdoor" | "mixed";

// --- Request ----------------------------------------------------------------

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
  /** Car fuel — petrol (default) or diesel; only meaningful for car mode. */
  fuel_type?: FuelType;
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

// --- Response: chosen_plan sub-objects --------------------------------------

export interface PlanDestination {
  id: number;
  city: string;
  country: string;
  latitude: number;
  longitude: number;
}

export interface CostBreakdown {
  transport: number;
  accommodation: number;
  activities: number;
}

export interface FlightEndpoint {
  code: string;
  city?: string | null;
  airport?: string | null;
}

export interface FlightLeg {
  date?: string | null; // ISO YYYY-MM-DD
  depart_time?: string | null;
  arrival_time?: string | null;
  duration_min?: number | null;
  stops?: number | null;
}

export interface FlightPlan {
  origin: FlightEndpoint;
  destination: FlightEndpoint;
  outbound?: FlightLeg | null;
  inbound?: FlightLeg | null;
  booking_url?: string | null;
}

export interface PlanTransport {
  mode: TransportMode;
  cost?: number | null;
  duration_min?: number | null;
  distance_km?: number | null;
  quality_flag: QualityFlag;
  /** Outbound departure clock time ("HH:MM") when the fare source provides one (air). */
  depart_time?: string | null;
  /** Rich per-leg flight detail (air mode only) for the flight card. */
  flight?: FlightPlan | null;
}

export interface PlanAccommodation {
  name?: string | null;
  accom_type?: string | null;
  price_per_night: number;
  rating?: number | null;
  latitude?: number | null;
  longitude?: number | null;
  quality_flag: QualityFlag;
  /** Listing detail (Inside Airbnb) for the card; present only for real listings. */
  listing_url?: string | null;
  picture_url?: string | null;
  review_count?: number | null;
  location_score?: number | null; // 0–5, Airbnb's review_scores_location
  neighbourhood?: string | null;
  amenities?: string[] | null;
  accommodates?: number | null; // guest capacity, matched to the party size
}

export interface ItineraryActivity {
  name: string;
  category?: string | null;
  indoor_outdoor: IndoorOutdoor;
  start_time?: string | null;
  end_time?: string | null;
  was_rescheduled: boolean;
  /** POI coordinates (from the Attraction). Optional — an unlocated activity isn't mapped. */
  latitude?: number | null;
  longitude?: number | null;
  /** OSM opening-hours string where the POI is tagged (sparse); null when unknown. */
  opening_hours?: string | null;
}

export interface ItineraryDay {
  day_number: number;
  activities: ItineraryActivity[];
}

export interface PlanWeatherDay {
  forecast_date: string;
  temp_min?: number | null;
  temp_max?: number | null;
  precip_prob?: number | null;
  conditions?: string | null;
  weather_conflict: boolean;
}

export interface ChosenPlan {
  destination: PlanDestination;
  overall_score: number;
  total_cost: number;
  /** Travel window (ISO dates), derived from the cheapest option — not user-chosen. */
  start_date?: string | null;
  end_date?: string | null;
  cost_breakdown: CostBreakdown;
  transport?: PlanTransport | null;
  accommodation?: PlanAccommodation | null;
  itinerary: ItineraryDay[];
  weather: PlanWeatherDay[];
}

// --- Response: ranked_candidates + meta -------------------------------------

export interface CandidateDestination {
  city: string;
  country: string;
}

export interface RankedCandidate {
  destination: CandidateDestination;
  total_cost?: number | null;
  overall_score?: number | null;
  transport_cost?: number | null;
  accommodation_cost?: number | null;
  experience_score?: number | null;
  weather_score?: number | null;
  preference_score?: number | null;
  feasible: boolean;
}

export interface TripMeta {
  trip_id?: number | null;
  generated_at: string;
  degraded_sources: string[];
  elapsed_ms?: number | null;
}

export interface TripResponse {
  chosen_plan: ChosenPlan;
  ranked_candidates: RankedCandidate[];
  /** LLM-generated; may be null when the explanation provider is disabled. */
  explanation?: string | null;
  meta: TripMeta;
}
