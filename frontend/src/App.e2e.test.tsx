/**
 * End-to-end frontend flow (input → results) against a mocked backend (BS Phase 11).
 *
 * Drives the real <App/> shell through the whole journey with the trip API stubbed for
 * determinism: fill the planner, submit, see the loading screen while the request is in
 * flight, then the Results page rendered from the mocked plan. Also covers the branch
 * outcomes the UI must handle — a server error (retry) and a no-plan result (adjust) — and
 * that client-side validation blocks a bad submit before any request is made. No network.
 */
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { ApiError } from "@/api/errors";
import { generateTrip } from "@/api/endpoints";
import type { TripResponse } from "@/types/trip";
import App from "./App";

vi.mock("@/api/wikipedia", () => ({
  fetchDestinationSummary: vi.fn(async () => null),
}));
vi.mock("@/api/endpoints", () => ({ generateTrip: vi.fn() }));

const mockGenerate = vi.mocked(generateTrip);

function planFixture(): TripResponse {
  return {
    chosen_plan: {
      destination: {
        id: 1,
        city: "Budapest",
        country: "Hungary",
        latitude: 47.4979,
        longitude: 19.0402,
      },
      overall_score: 0.92,
      total_cost: 780,
      cost_breakdown: { transport: 150, accommodation: 240, activities: 390 },
      transport: {
        mode: "car",
        cost: 150,
        duration_min: 600,
        distance_km: 1300,
        quality_flag: "real",
      },
      accommodation: {
        name: "Central flat",
        accom_type: "hotel",
        price_per_night: 80,
        rating: 4.6,
        latitude: 47.5,
        longitude: 19.05,
        quality_flag: "cached",
      },
      itinerary: [
        {
          day_number: 1,
          activities: [
            {
              name: "Buda Castle",
              category: "historic",
              indoor_outdoor: "outdoor",
              start_time: "10:00",
              end_time: "12:00",
              was_rescheduled: false,
            },
          ],
        },
      ],
      weather: [
        {
          forecast_date: "2026-09-12",
          temp_min: 14,
          temp_max: 24,
          precip_prob: 0.1,
          conditions: "Sunny",
          weather_conflict: false,
        },
      ],
    },
    ranked_candidates: [
      { destination: { city: "Budapest", country: "Hungary" }, feasible: true },
      { destination: { city: "Prague", country: "Czechia" }, feasible: true },
    ],
    explanation: "Budapest fits comfortably within your budget.",
    meta: { generated_at: "2026-09-10T00:00:00Z", degraded_sources: [] },
  };
}

function fillAndSubmit(origin = "Skopje") {
  fireEvent.change(screen.getByLabelText(/starting from/i), {
    target: { value: origin },
  });
  fireEvent.click(
    screen.getByRole("button", { name: /generate optimized plan/i }),
  );
}

beforeEach(() => {
  mockGenerate.mockReset();
});

describe("input → results E2E (mocked backend)", () => {
  it("submits the planner and renders the returned plan", async () => {
    mockGenerate.mockResolvedValue(planFixture());
    render(<App />);

    fillAndSubmit("Skopje");

    // Lands on Results, rendered from the mocked plan. The searching card lingers on
    // "Your trip is ready!" for ~800ms before navigating, so allow a little extra time.
    expect(
      await screen.findByRole(
        "heading",
        { name: /budapest, hungary/i },
        { timeout: 3000 },
      ),
    ).toBeInTheDocument();
    // The redesigned hero shows the match pill and a budget-aware "why" sentence (the LLM
    // explanation card was removed); the total and the day-1 stop render from the plan.
    expect(screen.getByText(/top match · 92\/100/i)).toBeInTheDocument();
    expect(screen.getByText(/€220 under/i)).toBeInTheDocument(); // 1000 budget − 780 total
    expect(screen.getAllByText(/€780/).length).toBeGreaterThan(0);
    expect(screen.getByText("Buda Castle")).toBeInTheDocument();

    // The client sent a well-formed request (defaults from the form).
    const payload = mockGenerate.mock.calls[0][0];
    expect(payload).toMatchObject({
      origin_label: "Skopje",
      transport_mode: "car",
      budget: 1000,
    });
  });

  it("shows the loading screen while the request is in flight", async () => {
    let resolve!: (r: TripResponse) => void;
    mockGenerate.mockReturnValue(
      new Promise<TripResponse>((r) => {
        resolve = r;
      }),
    );
    render(<App />);

    fillAndSubmit();
    // In-flight: the agent-progress screen is shown before the plan resolves.
    expect(await screen.findByText(/agents are on it/i)).toBeInTheDocument();

    resolve(planFixture());
    // ~800ms "Your trip is ready!" beat precedes navigation to Results.
    expect(
      await screen.findByRole(
        "heading",
        { name: /budapest, hungary/i },
        { timeout: 3000 },
      ),
    ).toBeInTheDocument();
  });

  it("surfaces a server error and retries the same request", async () => {
    mockGenerate.mockRejectedValue(new ApiError("INTERNAL_ERROR", "boom"));
    render(<App />);

    fillAndSubmit();
    expect(
      await screen.findByText(/something went wrong/i),
    ).toBeInTheDocument();

    // Fix the backend and retry — the same request is re-sent. Allow for the ~800ms
    // "Your trip is ready!" beat (and coverage slowing the run) before Results appears.
    mockGenerate.mockResolvedValue(planFixture());
    fireEvent.click(screen.getByRole("button", { name: /try again/i }));
    expect(
      await screen.findByRole(
        "heading",
        { name: /budapest, hungary/i },
        { timeout: 3000 },
      ),
    ).toBeInTheDocument();
    expect(mockGenerate).toHaveBeenCalledTimes(2);
  });

  it("shows the no-trip state and returns to the planner to adjust", async () => {
    mockGenerate.mockRejectedValue(
      new ApiError("NO_PLAN_AVAILABLE", "No feasible plan within your budget."),
    );
    render(<App />);

    fillAndSubmit();
    expect(
      await screen.findByText(/no trip fits these constraints/i),
    ).toBeInTheDocument();

    fireEvent.click(
      screen.getByRole("button", {
        name: /adjust your budget or preferences/i,
      }),
    );
    expect(
      screen.getByRole("button", { name: /generate optimized plan/i }),
    ).toBeInTheDocument();
  });

  it("blocks an invalid submit before any request is made", async () => {
    mockGenerate.mockResolvedValue(planFixture());
    render(<App />);

    // No origin entered → client-side validation stops the submit.
    fireEvent.click(
      screen.getByRole("button", { name: /generate optimized plan/i }),
    );
    expect(
      await screen.findByText(/where you're starting from/i),
    ).toBeInTheDocument();
    await waitFor(() => expect(mockGenerate).not.toHaveBeenCalled());
  });
});
