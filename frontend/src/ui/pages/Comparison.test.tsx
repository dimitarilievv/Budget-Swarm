import { fireEvent, render, screen, within } from "@testing-library/react";
import { vi } from "vitest";

import type {
  RankedCandidate,
  TripRequestPayload,
  TripResponse,
} from "@/types/trip";
import ComparisonPage from "./Comparison";

function candidate(
  over: Partial<RankedCandidate> & { city: string; country: string },
): RankedCandidate {
  const { city, country, ...rest } = over;
  return {
    destination: { city, country },
    total_cost: 390,
    overall_score: 0.8,
    transport_cost: 150,
    accommodation_cost: 240,
    experience_score: 1.0,
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
      total_cost: 391,
      start_date: "2026-10-22",
      end_date: "2026-10-26",
      cost_breakdown: { transport: 151, accommodation: 240, activities: 0 },
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

function request(): TripRequestPayload {
  return {
    budget: 1000,
    duration_days: 5,
    travelers: 2,
    transport_mode: "airplane",
    preferences: { culture: 1 },
    origin_label: "Skopje",
  };
}

function threeCities(): TripResponse {
  return fixture([
    candidate({
      city: "Budapest",
      country: "Hungary",
      overall_score: 0.92,
      transport_cost: 151,
      accommodation_cost: 240,
      preference_score: 0.88,
      weather_score: 0.75,
    }),
    candidate({ city: "Prague", country: "Czechia", overall_score: 0.85 }),
    // Sofia has no stay price and is over budget.
    candidate({
      city: "Sofia",
      country: "Bulgaria",
      overall_score: 0.6,
      transport_cost: 200,
      accommodation_cost: null,
      feasible: false,
    }),
  ]);
}

describe("ComparisonPage (city cards)", () => {
  it("renders a card per candidate, sorted by score", () => {
    render(<ComparisonPage response={threeCities()} request={request()} />);
    for (const city of ["Budapest", "Prague", "Sofia"]) {
      expect(screen.getByTestId(`city-${city}`)).toBeInTheDocument();
    }
    // Ranks run #1..#3 in score order.
    expect(
      within(screen.getByTestId("city-Budapest")).getByText("#1"),
    ).toBeInTheDocument();
    expect(
      within(screen.getByTestId("city-Sofia")).getByText("#3"),
    ).toBeInTheDocument();
  });

  it("marks exactly one Recommended card (the chosen destination)", () => {
    render(<ComparisonPage response={threeCities()} request={request()} />);
    expect(screen.getAllByText(/Recommended/i)).toHaveLength(1);
    expect(
      within(screen.getByTestId("city-Budapest")).getByText(/Recommended/i),
    ).toBeInTheDocument();
  });

  it("shows the score ×100 and the factor rows (no Experience row)", () => {
    render(<ComparisonPage response={threeCities()} request={request()} />);
    const buda = within(screen.getByTestId("city-Budapest"));
    expect(buda.getByText("92")).toBeInTheDocument(); // 0.92 -> 92
    expect(buda.getByText("Transport")).toBeInTheDocument();
    expect(buda.getByText("Stay")).toBeInTheDocument();
    expect(buda.getByText("Preferences")).toBeInTheDocument();
    expect(buda.getByText("Weather")).toBeInTheDocument();
    // Experience is intentionally omitted (same for every city).
    expect(screen.queryByText("Experience")).not.toBeInTheDocument();
    expect(screen.queryByText(/Experience score/i)).not.toBeInTheDocument();
  });

  it("shows the budget-aware total and under/over-budget note", () => {
    render(<ComparisonPage response={threeCities()} request={request()} />);
    const buda = within(screen.getByTestId("city-Budapest"));
    expect(buda.getByText("€391")).toBeInTheDocument(); // 151 + 240
    expect(buda.getByText(/€609 under budget/i)).toBeInTheDocument(); // 1000 - 391
  });

  it("handles a missing stay price without fabricating a total", () => {
    render(<ComparisonPage response={threeCities()} request={request()} />);
    const sofia = within(screen.getByTestId("city-Sofia"));
    expect(sofia.getByText("€200+")).toBeInTheDocument(); // transport only, "+"
    expect(sofia.getByText(/stay price missing/i)).toBeInTheDocument();
    expect(sofia.getByText("Not found")).toBeInTheDocument(); // Stay row
    expect(sofia.getByText("No stay found")).toBeInTheDocument(); // badge, not a button
  });

  it("gives only the recommended card an action button; others show a status badge", () => {
    render(<ComparisonPage response={threeCities()} request={request()} />);
    // The recommended (chosen) destination is the only one that can be opened.
    expect(
      within(screen.getByTestId("city-Budapest")).getByRole("button", {
        name: "View trip",
      }),
    ).toBeInTheDocument();
    // The others carry a non-interactive feasibility badge, not a button.
    const prague = within(screen.getByTestId("city-Prague"));
    expect(prague.queryByRole("button")).not.toBeInTheDocument();
    expect(prague.getByText("Fits your budget")).toBeInTheDocument();
    const sofia = within(screen.getByTestId("city-Sofia"));
    expect(sofia.queryByRole("button")).not.toBeInTheDocument();
    expect(sofia.getByText("No stay found")).toBeInTheDocument();
  });

  it("calls onAction from the recommended card's View trip button", () => {
    const onAction = vi.fn();
    render(
      <ComparisonPage
        response={threeCities()}
        request={request()}
        onAction={onAction}
      />,
    );
    fireEvent.click(
      within(screen.getByTestId("city-Budapest")).getByRole("button", {
        name: "View trip",
      }),
    );
    expect(onAction).toHaveBeenCalledTimes(1);
    expect(onAction.mock.calls[0][0].city).toBe("Budapest");
    expect(onAction.mock.calls[0][1]).toBe("View trip");
  });

  it("works without a request (no budget line, no under/over note)", () => {
    render(<ComparisonPage response={threeCities()} />);
    expect(screen.getByTestId("city-Budapest")).toBeInTheDocument();
    expect(screen.queryByText(/under budget/i)).not.toBeInTheDocument();
  });

  it("shows only the top 5 destinations by score", () => {
    const many = fixture(
      Array.from({ length: 8 }, (_, i) =>
        candidate({
          city: `City${i}`,
          country: "X",
          overall_score: 0.9 - i * 0.1, // City0 highest … City7 lowest
        }),
      ),
    );
    render(<ComparisonPage response={many} request={request()} />);
    expect(screen.getAllByText(/^#\d$/)).toHaveLength(5); // five ranked cards
    expect(screen.getByTestId("city-City0")).toBeInTheDocument(); // top
    expect(screen.getByTestId("city-City4")).toBeInTheDocument(); // 5th
    expect(screen.queryByTestId("city-City5")).not.toBeInTheDocument(); // 6th dropped
  });

  it("shows an empty state when there are no candidates", () => {
    render(<ComparisonPage response={fixture([])} request={request()} />);
    expect(
      screen.getByText(/no candidate destinations to compare/i),
    ).toBeInTheDocument();
  });

  it("calls onBack from the back link", () => {
    const onBack = vi.fn();
    render(
      <ComparisonPage
        response={threeCities()}
        request={request()}
        onBack={onBack}
      />,
    );
    fireEvent.click(screen.getByRole("button", { name: /back to budapest/i }));
    expect(onBack).toHaveBeenCalledTimes(1);
  });
});
