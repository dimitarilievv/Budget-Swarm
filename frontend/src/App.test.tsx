import { fireEvent, render, screen, within } from "@testing-library/react";
import { vi } from "vitest";

import App from "./App";

// Hermetic: stub the trip API and the Wikipedia enrichment so no network is touched.
vi.mock("@/api/wikipedia", () => ({
  fetchDestinationSummary: vi.fn(async () => null),
}));

vi.mock("@/api/endpoints", () => ({
  generateTrip: vi.fn(async () => ({
    chosen_plan: {
      destination: {
        id: 1,
        city: "Budapest",
        country: "Hungary",
        latitude: 47.5,
        longitude: 19,
      },
      overall_score: 0.9,
      total_cost: 800,
      cost_breakdown: { transport: 150, accommodation: 240, activities: 410 },
      transport: null,
      accommodation: null,
      itinerary: [],
      weather: [],
    },
    ranked_candidates: [
      { destination: { city: "Budapest", country: "Hungary" }, feasible: true },
      { destination: { city: "Prague", country: "Czechia" }, feasible: true },
    ],
    explanation: "It fits your budget.",
    meta: { generated_at: "2026-01-01T00:00:00Z", degraded_sources: [] },
  })),
}));

describe("App", () => {
  it("renders the hero headline and the brand", () => {
    render(<App />);
    expect(
      screen.getByRole("heading", { name: /tell us your budget/i }),
    ).toBeInTheDocument();
    expect(
      screen.getByRole("button", { name: /budgetswarm home/i }),
    ).toBeVisible();
  });

  it("renders the trip input form's CTA", () => {
    render(<App />);
    expect(
      screen.getByRole("button", { name: /generate optimized plan/i }),
    ).toBeInTheDocument();
  });

  it("renders the home header nav", () => {
    render(<App />);
    // The home page's sticky header (banner) has anchor nav links and a Profile pill.
    const header = within(screen.getByRole("banner"));
    expect(
      header.getByRole("link", { name: /plan trip/i }),
    ).toBeInTheDocument();
    expect(
      header.getByRole("link", { name: /how it works/i }),
    ).toBeInTheDocument();
    expect(
      header.getByRole("button", { name: /profile/i }),
    ).toBeInTheDocument();
  });

  it("navigates Home → Results → Compare → back after generating a trip", async () => {
    render(<App />);

    // Fill the required origin and generate.
    fireEvent.change(screen.getByLabelText(/starting from/i), {
      target: { value: "Skopje" },
    });
    fireEvent.click(
      screen.getByRole("button", { name: /generate optimized plan/i }),
    );

    // Results page appears. Allow for the ~800ms "Your trip is ready!" beat (and coverage
    // instrumentation slowing the run) before the searching card hands off to Results.
    expect(
      await screen.findByRole(
        "heading",
        { name: /budapest, hungary/i },
        { timeout: 3000 },
      ),
    ).toBeInTheDocument();

    // The Navbar "Compare" item is enabled once there are candidates.
    expect(screen.getByRole("button", { name: /^compare$/i })).toBeEnabled();

    // Go to the comparison surface via the Results action.
    fireEvent.click(
      screen.getByRole("button", { name: /compare destinations/i }),
    );
    expect(
      screen.getByRole("heading", { name: /compare destinations/i }),
    ).toBeInTheDocument();

    // And back to the results (the compare page's back link names the recommended city).
    fireEvent.click(screen.getByRole("button", { name: /back to/i }));
    expect(
      screen.getByRole("heading", { name: /budapest, hungary/i }),
    ).toBeInTheDocument();
  });
});
