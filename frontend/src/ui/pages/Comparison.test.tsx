import { fireEvent, render, screen, within } from "@testing-library/react";
import { vi } from "vitest";

import type { RankedCandidate, TripResponse } from "@/types/trip";
import ComparisonPage from "./Comparison";

function candidate(
  over: Partial<RankedCandidate> & {
    city: string;
    country: string;
  },
): RankedCandidate {
  const { city, country, ...rest } = over;
  return {
    destination: { city, country },
    total_cost: 800,
    overall_score: 0.8,
    transport_cost: 150,
    accommodation_cost: 240,
    experience_score: 0.8,
    weather_score: 0.7,
    preference_score: 0.9,
    feasible: true,
    ...rest,
  };
}

function fixture(candidates: RankedCandidate[]): TripResponse {
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
      cost_breakdown: { transport: 151, accommodation: 240, activities: 389 },
      transport: null,
      accommodation: null,
      itinerary: [],
      weather: [],
    },
    ranked_candidates: candidates,
    explanation: null,
    meta: { generated_at: "2026-09-10T00:00:00Z", degraded_sources: [] },
  };
}

function threeCandidates(): TripResponse {
  return fixture([
    candidate({
      city: "Budapest",
      country: "Hungary",
      total_cost: 780,
      overall_score: 0.92,
      transport_cost: 151,
      accommodation_cost: 240,
      experience_score: 0.88,
      weather_score: 0.75,
      preference_score: 0.92,
      feasible: true,
    }),
    candidate({
      city: "Prague",
      country: "Czechia",
      total_cost: 900,
      overall_score: 0.85,
      feasible: true,
    }),
    candidate({
      city: "Sofia",
      country: "Bulgaria",
      total_cost: 1200,
      overall_score: 0.6,
      feasible: false,
    }),
  ]);
}

describe("ComparisonPage", () => {
  it("renders every documented column", () => {
    render(<ComparisonPage response={threeCandidates()} />);
    for (const label of [
      "Total cost",
      "Overall score",
      "Transport cost",
      "Accommodation cost",
      "Experience score",
      "Weather score",
      "Preference score",
      "Feasible within budget",
    ]) {
      expect(screen.getByText(label)).toBeInTheDocument();
    }
  });

  it("renders each candidate destination", () => {
    render(<ComparisonPage response={threeCandidates()} />);
    expect(screen.getByTestId("candidate-Budapest")).toBeInTheDocument();
    expect(screen.getByTestId("candidate-Prague")).toBeInTheDocument();
    expect(screen.getByTestId("candidate-Sofia")).toBeInTheDocument();
  });

  it("flags infeasible candidates instead of hiding them", () => {
    render(<ComparisonPage response={threeCandidates()} />);
    // Sofia is over budget: shown, and marked "No".
    const sofia = within(screen.getByTestId("candidate-Sofia"));
    expect(sofia.getByText("No")).toBeInTheDocument();
    // The two feasible candidates read "Yes".
    expect(screen.getAllByText("Yes")).toHaveLength(2);
  });

  it("highlights the chosen destination with a single Recommended badge", () => {
    render(<ComparisonPage response={threeCandidates()} />);
    const badges = screen.getAllByText(/Recommended/i);
    expect(badges).toHaveLength(1);
    // The badge sits in the Budapest (chosen) column.
    const budapest = within(screen.getByTestId("candidate-Budapest"));
    expect(budapest.getByText(/Recommended/i)).toBeInTheDocument();
  });

  it("shows factor scores and the /100 overall score", () => {
    render(<ComparisonPage response={threeCandidates()} />);
    const budapest = within(screen.getByTestId("candidate-Budapest"));
    expect(budapest.getByText("92")).toBeInTheDocument(); // overall 0.92 -> 92
    expect(budapest.getByText("0.88")).toBeInTheDocument(); // experience score
    expect(budapest.getByText(/€780/)).toBeInTheDocument(); // total cost
    expect(budapest.getByText(/€151/)).toBeInTheDocument(); // transport cost
  });

  it("renders an em dash for missing values", () => {
    const r = fixture([
      candidate({
        city: "Riga",
        country: "Latvia",
        overall_score: null,
        transport_cost: null,
      }),
    ]);
    render(<ComparisonPage response={r} />);
    const riga = within(screen.getByTestId("candidate-Riga"));
    expect(riga.getAllByText("—").length).toBeGreaterThan(0);
  });

  it("shows an empty state when there are no candidates", () => {
    render(<ComparisonPage response={fixture([])} />);
    expect(
      screen.getByText(/no candidate destinations to compare/i),
    ).toBeInTheDocument();
  });

  it("calls onBack from the back button", () => {
    const onBack = vi.fn();
    render(<ComparisonPage response={threeCandidates()} onBack={onBack} />);
    fireEvent.click(screen.getByRole("button", { name: /back to results/i }));
    expect(onBack).toHaveBeenCalledTimes(1);
  });
});
