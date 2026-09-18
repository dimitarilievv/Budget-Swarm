import { AxiosError, type AxiosInstance, type AxiosResponse } from "axios";

import { ApiError } from "./errors";
import { generateTrip, getTrip } from "./trips";
import type { ChosenPlan, TripRequestPayload, TripResponse } from "./types";

const REQUEST: TripRequestPayload = {
  budget: 1000,
  duration_days: 5,
  travelers: 2,
  transport_mode: "car",
  preferences: { culture: 0.9 },
  origin_label: "Skopje",
  origin_lat: 41.997,
  origin_lon: 21.428,
  vehicle_consumption: 6.5,
};

const CHOSEN_PLAN: ChosenPlan = {
  destination: {
    id: 12,
    city: "Budapest",
    country: "Hungary",
    latitude: 47.5,
    longitude: 19.0,
  },
  overall_score: 0.87,
  total_cost: 812.5,
  cost_breakdown: { transport: 151, accommodation: 240, activities: 421.5 },
  transport: {
    mode: "car",
    cost: 151,
    duration_min: 780,
    distance_km: 1300,
    quality_flag: "estimated",
  },
  accommodation: null,
  itinerary: [
    {
      day_number: 1,
      activities: [
        {
          name: "Buda Castle",
          category: "historic",
          indoor_outdoor: "outdoor",
          start_time: "10:00",
          end_time: "12:30",
          was_rescheduled: false,
        },
      ],
    },
  ],
  weather: [],
};

const TRIP_RESPONSE: TripResponse = {
  chosen_plan: CHOSEN_PLAN,
  ranked_candidates: [
    {
      destination: { city: "Budapest", country: "Hungary" },
      overall_score: 0.87,
      feasible: true,
    },
    {
      destination: { city: "Prague", country: "Czechia" },
      overall_score: 0.81,
      feasible: false,
    },
  ],
  explanation: "Budapest fits your budget and preferences.",
  meta: {
    trip_id: 341,
    generated_at: "2026-08-15T10:22:00Z",
    degraded_sources: [],
    elapsed_ms: 2140,
  },
};

/** A client whose post/get resolve with `data`, recording the call. */
function okClient(data: unknown, calls: unknown[][] = []): AxiosInstance {
  const handler = async (...args: unknown[]) => {
    calls.push(args);
    return { data } as AxiosResponse;
  };
  return { post: handler, get: handler } as unknown as AxiosInstance;
}

/** A client whose post/get reject with `error`. */
function failingClient(error: unknown): AxiosInstance {
  const handler = async () => {
    throw error;
  };
  return { post: handler, get: handler } as unknown as AxiosInstance;
}

function axiosError(status: number, data: unknown): AxiosError {
  return new AxiosError("Request failed", "ERR_BAD_RESPONSE", undefined, {}, {
    status,
    data,
  } as AxiosResponse);
}

describe("generateTrip", () => {
  it("posts the payload and returns the parsed response", async () => {
    const calls: unknown[][] = [];
    const result = await generateTrip(REQUEST, okClient(TRIP_RESPONSE, calls));
    expect(result).toEqual(TRIP_RESPONSE);
    expect(calls[0][0]).toBe("/api/generate-trip");
    expect(calls[0][1]).toEqual(REQUEST);
  });

  it("surfaces the 503 NO_PLAN_AVAILABLE envelope as an ApiError", async () => {
    const err = axiosError(503, {
      error: { code: "NO_PLAN_AVAILABLE", message: "no feasible plan" },
    });
    await expect(
      generateTrip(REQUEST, failingClient(err)),
    ).rejects.toMatchObject({
      code: "NO_PLAN_AVAILABLE",
      status: 503,
    });
  });

  it("surfaces a validation error with its field", async () => {
    const err = axiosError(422, {
      error: {
        code: "VALIDATION_ERROR",
        message: "budget must be > 0",
        field: "budget",
      },
    });
    try {
      await generateTrip(REQUEST, failingClient(err));
      throw new Error("should have thrown");
    } catch (e) {
      expect(e).toBeInstanceOf(ApiError);
      const api = e as ApiError;
      expect(api.code).toBe("VALIDATION_ERROR");
      expect(api.field).toBe("budget");
      expect(api.message).toBe("budget must be > 0");
    }
  });

  it("maps a connection failure to NETWORK_ERROR", async () => {
    const err = new AxiosError("Network Error", "ERR_NETWORK"); // no response
    await expect(
      generateTrip(REQUEST, failingClient(err)),
    ).rejects.toMatchObject({
      code: "NETWORK_ERROR",
    });
  });

  it("falls back to INTERNAL_ERROR for a non-enveloped error body", async () => {
    const err = axiosError(500, "<html>oops</html>");
    await expect(
      generateTrip(REQUEST, failingClient(err)),
    ).rejects.toMatchObject({
      code: "INTERNAL_ERROR",
      status: 500,
    });
  });
});

describe("getTrip", () => {
  it("fetches a stored plan by id", async () => {
    const calls: unknown[][] = [];
    const plan = await getTrip(341, okClient(CHOSEN_PLAN, calls));
    expect(plan.destination.city).toBe("Budapest");
    expect(calls[0][0]).toBe("/api/trips/341");
  });

  it("surfaces a 404 as NOT_FOUND", async () => {
    const err = axiosError(404, {
      error: { code: "NOT_FOUND", message: "no trip with id 999" },
    });
    await expect(getTrip(999, failingClient(err))).rejects.toMatchObject({
      code: "NOT_FOUND",
      status: 404,
    });
  });
});
