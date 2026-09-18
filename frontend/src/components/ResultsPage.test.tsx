import { fireEvent, render, screen, within } from "@testing-library/react";
import { vi } from "vitest";

import type { TripResponse } from "../api/types";
import ResultsPage from "./ResultsPage";

function fixture(): TripResponse {
  return {
    chosen_plan: {
      destination: {
        id: 7,
        city: "Budapest",
        country: "Hungary",
        latitude: 47.4979,
        longitude: 19.0402,
      },
      overall_score: 0.92,
      total_cost: 842,
      cost_breakdown: {
        transport: 245,
        accommodation: 320,
        activities: 277,
      },
      transport: {
        mode: "car",
        cost: 245,
        duration_min: 375,
        distance_km: 640,
        quality_flag: "real",
      },
      accommodation: {
        name: "Riverview Boutique",
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
              name: "St. Stephen's Basilica",
              category: "culture",
              indoor_outdoor: "indoor",
              start_time: "10:00",
              end_time: "11:30",
              was_rescheduled: false,
            },
            {
              name: "Danube river walk",
              category: "nature",
              indoor_outdoor: "outdoor",
              start_time: "14:00",
              end_time: "16:00",
              was_rescheduled: true,
            },
          ],
        },
        {
          day_number: 2,
          activities: [],
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
        {
          forecast_date: "2026-09-13",
          temp_min: 12,
          temp_max: 18,
          precip_prob: 0.8,
          conditions: "Rain",
          weather_conflict: true,
        },
      ],
    },
    ranked_candidates: [],
    explanation: "Best value among the candidates.",
    meta: { generated_at: "2026-09-10T00:00:00Z", degraded_sources: [] },
  };
}

describe("ResultsPage", () => {
  it("renders the destination, score and estimated total", () => {
    render(<ResultsPage response={fixture()} />);
    expect(
      screen.getByRole("heading", { name: "Budapest" }),
    ).toBeInTheDocument();
    expect(screen.getAllByText(/Hungary/i).length).toBeGreaterThan(0);
    expect(screen.getByText("92")).toBeInTheDocument(); // 0.92 -> /100
    expect(screen.getAllByText(/€842/).length).toBeGreaterThan(0);
  });

  it("shows the cost breakdown for every category", () => {
    render(<ResultsPage response={fixture()} />);
    const card = screen
      .getByRole("heading", { name: "Cost breakdown" })
      .closest("section") as HTMLElement;
    const scoped = within(card);
    expect(scoped.getByText("Transport")).toBeInTheDocument();
    expect(scoped.getByText("Accommodation")).toBeInTheDocument();
    expect(scoped.getByText("Activities")).toBeInTheDocument();
    expect(scoped.getByText(/€245/)).toBeInTheDocument();
    expect(scoped.getByText(/€320/)).toBeInTheDocument();
    expect(scoped.getByText(/€277/)).toBeInTheDocument();
  });

  it("renders transport and accommodation details", () => {
    render(<ResultsPage response={fixture()} />);
    expect(screen.getByText("Riverview Boutique")).toBeInTheDocument();
    expect(screen.getByText(/640 km/)).toBeInTheDocument();
    expect(screen.getByText(/6 h 15 min/)).toBeInTheDocument(); // 375 min
    expect(screen.getByText(/★ 4.6/)).toBeInTheDocument();
  });

  it("shows the itinerary per day with indoor/outdoor and rescheduled markers", () => {
    render(<ResultsPage response={fixture()} />);
    expect(screen.getByText(/Day 1/i)).toBeInTheDocument();
    expect(screen.getByText(/Day 2/i)).toBeInTheDocument();
    // Indoor/outdoor tags.
    expect(screen.getByText("Indoor")).toBeInTheDocument();
    expect(screen.getByText("Outdoor")).toBeInTheDocument();
    // The rescheduled activity carries a marker.
    expect(screen.getByText(/rescheduled/i)).toBeInTheDocument();
    // An empty day is shown as free.
    expect(screen.getByText(/free day/i)).toBeInTheDocument();
  });

  it("summarises the weather and flags a conflict day", () => {
    render(<ResultsPage response={fixture()} />);
    expect(screen.getByText("Sunny")).toBeInTheDocument();
    expect(screen.getByText("Rain")).toBeInTheDocument();
    expect(screen.getByText(/rain risk/i)).toBeInTheDocument();
    expect(screen.getByText(/14–24°C/)).toBeInTheDocument();
    expect(screen.getByText(/80% precip/)).toBeInTheDocument();
  });

  it("renders the explanation as 'Why {city} won'", () => {
    render(<ResultsPage response={fixture()} />);
    expect(screen.getByText(/why budapest won/i)).toBeInTheDocument();
    expect(
      screen.getByText(/best value among the candidates/i),
    ).toBeInTheDocument();
  });

  it("calls onBack from the back button", () => {
    const onBack = vi.fn();
    render(<ResultsPage response={fixture()} onBack={onBack} />);
    fireEvent.click(screen.getByRole("button", { name: /back to planning/i }));
    expect(onBack).toHaveBeenCalledTimes(1);
  });

  it("degrades gracefully when transport and accommodation are absent", () => {
    const r = fixture();
    r.chosen_plan.transport = null;
    r.chosen_plan.accommodation = null;
    render(<ResultsPage response={r} />);
    expect(
      screen.getByText(/no transport details available/i),
    ).toBeInTheDocument();
    expect(
      screen.getByText(/no accommodation details available/i),
    ).toBeInTheDocument();
  });

  it("does not render the explanation section when absent", () => {
    const r = fixture();
    r.explanation = null;
    render(<ResultsPage response={r} />);
    expect(screen.queryByText(/why .* won/i)).not.toBeInTheDocument();
  });

  it("marks the total row in the cost table", () => {
    render(<ResultsPage response={fixture()} />);
    const totalRow = screen.getByText("Total").closest("tr");
    expect(totalRow).not.toBeNull();
    expect(
      within(totalRow as HTMLElement).getByText(/€842/),
    ).toBeInTheDocument();
  });
});
