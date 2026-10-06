import { fireEvent, render, screen } from "@testing-library/react";
import { vi } from "vitest";

import type { TripRequestPayload, TripResponse } from "@/types/trip";
import ResultsPage from "./Results";

// Keep tests hermetic: the hero fetches a city photo from Wikipedia, so stub the network call.
vi.mock("@/api/wikipedia", () => ({
  fetchDestinationSummary: vi.fn(async () => null),
}));

function request(): TripRequestPayload {
  return {
    budget: 1000,
    duration_days: 5,
    travelers: 2,
    transport_mode: "airplane",
    preferences: { culture: 1, food: 0.6, tourism: 0.4 },
    origin_label: "Skopje",
  };
}

function fixture(): TripResponse {
  return {
    chosen_plan: {
      destination: {
        id: 7,
        city: "Istanbul",
        country: "Turkey",
        latitude: 41.0082,
        longitude: 28.9784,
      },
      overall_score: 0.85,
      total_cost: 366,
      start_date: "2026-10-22",
      end_date: "2026-10-26",
      cost_breakdown: { transport: 178, accommodation: 188, activities: 0 },
      transport: {
        mode: "airplane",
        cost: 178,
        duration_min: 175,
        distance_km: 1276,
        quality_flag: "real",
        flight: {
          origin: { code: "SKP", city: "Skopje", airport: "Skopje Airport" },
          destination: {
            code: "SAW",
            city: "Istanbul",
            airport: "Istanbul Sabiha Gökçen",
          },
          outbound: {
            date: "2026-10-22",
            depart_time: "14:20",
            arrival_time: "15:50",
            duration_min: 90,
            stops: 0,
          },
          inbound: {
            date: "2026-10-26",
            depart_time: "17:15",
            arrival_time: "18:40",
            duration_min: 85,
            stops: 0,
          },
          booking_url: "https://www.aviasales.com/search/SKP2210SAW26102",
        },
      },
      accommodation: {
        name: "Kadıköy Mona Lisa",
        accom_type: "entire home",
        price_per_night: 47,
        rating: 4.8,
        latitude: 40.99,
        longitude: 29.03,
        quality_flag: "cached",
        listing_url: "https://www.airbnb.com/rooms/12345",
        picture_url: "https://example.com/stay.jpg",
        review_count: 32,
        location_score: 4.8,
        neighbourhood: "Kadıköy",
        amenities: ["Wi-Fi", "Kitchen", "AC", "Heating", "Washer"],
        accommodates: 2,
      },
      itinerary: [
        {
          day_number: 1,
          activities: [
            {
              // Arrival day: starts 09:00, before the 15:50 landing → a timing conflict.
              name: "The Hagia Sophia Grand Mosque",
              category: "religion",
              indoor_outdoor: "indoor",
              start_time: "09:00",
              end_time: "11:00",
              was_rescheduled: false,
              latitude: 41.0086,
              longitude: 28.98,
            },
            {
              // Near-duplicate of the first stop → should be deduped away.
              name: "Hagia Sophia Grand Mosque",
              category: "religion",
              indoor_outdoor: "indoor",
              start_time: "11:00",
              end_time: "13:00",
              was_rescheduled: false,
            },
            {
              name: "Istanbul Archaeology Museums",
              category: "national_museums",
              indoor_outdoor: "indoor",
              start_time: "13:00",
              end_time: "15:00",
              was_rescheduled: false,
            },
          ],
        },
        {
          day_number: 2,
          activities: [
            {
              name: "Basilica Cistern",
              category: "historic",
              indoor_outdoor: "outdoor",
              start_time: "10:00",
              end_time: "11:30",
              was_rescheduled: false,
            },
          ],
        },
      ],
      weather: [
        {
          forecast_date: "2026-10-22",
          temp_min: 12,
          temp_max: 24,
          precip_prob: 10,
          conditions: "Sunny",
          weather_conflict: false,
        },
        {
          forecast_date: "2026-10-23",
          temp_min: 11,
          temp_max: 18,
          precip_prob: 80,
          conditions: "Rain",
          weather_conflict: true,
        },
      ],
    },
    ranked_candidates: [
      { destination: { city: "Istanbul", country: "Turkey" }, feasible: true },
      { destination: { city: "Vienna", country: "Austria" }, feasible: true },
    ],
    explanation: "Istanbul fits comfortably within your budget.",
    meta: { generated_at: "2026-09-10T00:00:00Z", degraded_sources: [] },
  };
}

describe("ResultsPage (redesign)", () => {
  it("renders the hero: city, match score, dates and the estimated total", () => {
    render(<ResultsPage response={fixture()} request={request()} />);
    expect(
      screen.getByRole("heading", { name: /istanbul, turkey/i }),
    ).toBeInTheDocument();
    expect(screen.getByText(/top match · 85\/100/i)).toBeInTheDocument();
    expect(screen.getByText(/Next best: Vienna/i)).toBeInTheDocument();
    expect(screen.getAllByText(/€366/).length).toBeGreaterThan(0);
  });

  it("writes a budget-aware 'why' sentence from the request", () => {
    render(<ResultsPage response={fixture()} request={request()} />);
    expect(screen.getByText(/picks best/i)).toBeInTheDocument();
    expect(screen.getByText(/€634 under/i)).toBeInTheDocument(); // the under-budget clause
    // €634 left (1000 − 366) surfaces in the budget bar caption.
    expect(screen.getByText(/€634 left/i)).toBeInTheDocument();
  });

  it("shows the estimated chip when a figure is not fully live", () => {
    // Accommodation is 'cached' in the fixture, so the chip shows.
    render(<ResultsPage response={fixture()} request={request()} />);
    expect(screen.getByText(/some figures estimated/i)).toBeInTheDocument();
  });

  it("hides the estimated chip when everything is live and nothing degraded", () => {
    const r = fixture();
    r.chosen_plan.accommodation!.quality_flag = "real";
    render(<ResultsPage response={r} request={request()} />);
    expect(
      screen.queryByText(/some figures estimated/i),
    ).not.toBeInTheDocument();
  });

  it("summarises flights and stay, omitting the activities tile when free", () => {
    render(<ResultsPage response={fixture()} request={request()} />);
    expect(screen.getAllByText(/€178/).length).toBeGreaterThan(0); // flights
    expect(screen.getAllByText(/€188/).length).toBeGreaterThan(0); // stay
    // Activities are free sights (€0), so the summary tile is omitted entirely.
    expect(screen.queryByText("Activities")).not.toBeInTheDocument();
    expect(screen.queryByText(/free sights/i)).not.toBeInTheDocument();
  });

  it("renders the flights card with both legs and a reserve button", () => {
    render(<ResultsPage response={fixture()} request={request()} />);
    expect(screen.getByText("14:20")).toBeInTheDocument();
    expect(screen.getByText("18:40")).toBeInTheDocument();
    // Reserve is a gated action (sign-in required), so it's a button, not a raw link —
    // the booking URL is never exposed in the DOM for an anonymous viewer.
    expect(
      screen.getByRole("button", { name: /reserve flight/i }),
    ).toBeInTheDocument();
  });

  it("renders the stay with amenities (+N), nights total, reserve + map buttons", () => {
    render(<ResultsPage response={fixture()} request={request()} />);
    expect(screen.getByText("Kadıköy Mona Lisa")).toBeInTheDocument();
    expect(screen.getByText("Wi-Fi")).toBeInTheDocument();
    expect(screen.getByText("+1")).toBeInTheDocument(); // 5 amenities → 4 shown + "+1"
    expect(screen.getByText(/× 4 nights/i)).toBeInTheDocument(); // 188 / 47 = 4
    expect(screen.getByText(/★ 4.8/)).toBeInTheDocument();
    // The booking action is a sign-in-gated button (not a raw link).
    expect(
      screen.getByRole("button", { name: /reserve accommodation/i }),
    ).toBeInTheDocument();
    // The stay has coordinates, so it offers a "Show on map" button.
    expect(
      screen.getByRole("button", { name: /show on map/i }),
    ).toBeInTheDocument();
  });

  it("builds a step-by-step journey with fly-out, days and fly-home", () => {
    render(<ResultsPage response={fixture()} request={request()} />);
    expect(screen.getByText("Your trip, step by step")).toBeInTheDocument();
    expect(
      screen.getByRole("button", { name: /fly out/i }),
    ).toBeInTheDocument();
    expect(
      screen.getByRole("button", { name: /fly home/i }),
    ).toBeInTheDocument();
    // Day 1 is selected by default → its stops show.
    expect(screen.getByRole("button", { name: /day 1/i })).toBeInTheDocument();
  });

  it("dedupes near-identical stops and cleans category labels", () => {
    render(<ResultsPage response={fixture()} request={request()} />);
    // "The Hagia Sophia Grand Mosque" + "Hagia Sophia Grand Mosque" collapse to one row.
    expect(screen.getAllByText(/Hagia Sophia Grand Mosque/).length).toBe(1);
    // national_museums → "Museum" (shown in the stop's "duration · category" line).
    expect(screen.getByText(/· Museum/)).toBeInTheDocument();
  });

  it("notes the arrival time on day 1 instead of warning about conflicts", () => {
    render(<ResultsPage response={fixture()} request={request()} />);
    // The engine schedules stops after arrival, so day 1 shows an informative arrival note,
    // not a "move them" warning or a "Check timing" flag.
    expect(screen.getByText(/you land at SAW at 15:50/i)).toBeInTheDocument();
    expect(screen.queryByText(/check timing/i)).not.toBeInTheDocument();
    expect(screen.queryByText(/move them/i)).not.toBeInTheDocument();
  });

  it("shows the flight detail when a flight step is clicked", () => {
    render(<ResultsPage response={fixture()} request={request()} />);
    fireEvent.click(screen.getByRole("button", { name: /fly home/i }));
    expect(screen.getByText(/return flights total/i)).toBeInTheDocument();
    expect(
      screen.getByRole("button", { name: /reserve flights/i }),
    ).toBeInTheDocument();
  });

  it("renders the weather with temperatures and precipitation", () => {
    render(<ResultsPage response={fixture()} request={request()} />);
    expect(screen.getByText("24°")).toBeInTheDocument();
    expect(screen.getByText(/80% rain/i)).toBeInTheDocument();
  });

  it("shows an honest note instead of an empty weather card", () => {
    const r = fixture();
    r.chosen_plan.weather = [];
    render(<ResultsPage response={r} request={request()} />);
    expect(
      screen.getByText(/isn't available for these dates yet/i),
    ).toBeInTheDocument();
    expect(
      screen.queryByText(/no forecast available/i),
    ).not.toBeInTheDocument();
  });

  it("labels far-future weather as typical conditions with a caveat", () => {
    const r = fixture();
    // 30 days out — beyond the live-forecast horizon.
    r.chosen_plan.start_date = new Date(Date.now() + 30 * 86400000)
      .toISOString()
      .slice(0, 10);
    render(<ResultsPage response={r} request={request()} />);
    // "Typical conditions" appears on both the badge and the caveat.
    expect(screen.getAllByText(/typical conditions/i).length).toBeGreaterThan(
      0,
    );
    expect(screen.getByText(/averaged from recent years/i)).toBeInTheDocument();
    // The days still render (temperatures present), just labelled as typical.
    expect(screen.getByText("24°")).toBeInTheDocument();
  });

  it("labels near-term weather as a live forecast", () => {
    const r = fixture();
    r.chosen_plan.start_date = new Date(Date.now() + 5 * 86400000)
      .toISOString()
      .slice(0, 10);
    render(<ResultsPage response={r} request={request()} />);
    expect(screen.getByText(/live forecast/i)).toBeInTheDocument();
  });

  it("calls onBack and onPlanAnother from their buttons", () => {
    const onBack = vi.fn();
    const onPlanAnother = vi.fn();
    render(
      <ResultsPage
        response={fixture()}
        request={request()}
        onBack={onBack}
        onPlanAnother={onPlanAnother}
      />,
    );
    fireEvent.click(screen.getByRole("button", { name: /back to planning/i }));
    fireEvent.click(screen.getByRole("button", { name: /plan another trip/i }));
    expect(onBack).toHaveBeenCalledTimes(1);
    expect(onPlanAnother).toHaveBeenCalledTimes(1);
  });

  it("works without a request (degrades the budget-dependent bits)", () => {
    render(<ResultsPage response={fixture()} />);
    // Still renders the hero and total; no "under your budget" clause without a budget.
    expect(
      screen.getByRole("heading", { name: /istanbul, turkey/i }),
    ).toBeInTheDocument();
    expect(screen.queryByText(/under your/i)).not.toBeInTheDocument();
  });

  it("degrades gracefully when transport and accommodation are absent", () => {
    const r = fixture();
    r.chosen_plan.transport = null;
    r.chosen_plan.accommodation = null;
    render(<ResultsPage response={r} request={request()} />);
    expect(
      screen.getByText(/no transport details available/i),
    ).toBeInTheDocument();
    expect(
      screen.getByText(/no accommodation details available/i),
    ).toBeInTheDocument();
  });

  it("shows the saved-trip banner + Refresh and hides Save for a clean snapshot", () => {
    const r = fixture();
    r.meta.trip_id = 5; // a stored snapshot carries its trip id
    render(
      <ResultsPage
        response={r}
        request={request()}
        saved={{ tripId: 5, savedAt: "2026-01-02T00:00:00Z" }}
      />,
    );
    expect(screen.getByText(/saved on/i)).toBeInTheDocument();
    expect(
      screen.getByRole("button", { name: /refresh prices/i }),
    ).toBeInTheDocument();
    // Already saved → no "Save trip" button.
    expect(
      screen.queryByRole("button", { name: /^save trip$/i }),
    ).not.toBeInTheDocument();
  });

  it("prompts to update after a refresh (saved ref, but the plan isn't persisted)", () => {
    const r = fixture(); // no meta.trip_id → a refreshed, unsaved plan
    render(
      <ResultsPage
        response={r}
        request={request()}
        saved={{ tripId: 5, savedAt: "2026-01-02T00:00:00Z" }}
      />,
    );
    expect(screen.getByText(/showing refreshed prices/i)).toBeInTheDocument();
    expect(
      screen.getByRole("button", { name: /update saved trip/i }),
    ).toBeInTheDocument();
  });
});
