import { fireEvent, render, screen, within } from "@testing-library/react";
import { vi } from "vitest";

import type { TripResponse } from "@/types/trip";
import ResultsPage from "./Results";

// Keep tests hermetic: the Results page enriches from Wikipedia, so stub the network call.
vi.mock("@/api/wikipedia", () => ({
  fetchDestinationSummary: vi.fn(async () => null),
}));

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
      start_date: "2026-12-01",
      end_date: "2026-12-05",
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
        depart_time: "10:15",
      },
      accommodation: {
        name: "Riverview Boutique",
        accom_type: "hotel",
        price_per_night: 80,
        rating: 4.6,
        latitude: 47.5,
        longitude: 19.05,
        quality_flag: "cached",
        listing_url: "https://www.airbnb.com/rooms/12345",
        picture_url: "https://example.com/photo.jpg",
        review_count: 312,
        location_score: 4.8,
        neighbourhood: "District V",
        amenities: ["Wi-Fi", "Kitchen", "AC"],
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
              latitude: 47.5008,
              longitude: 19.0537,
            },
            {
              name: "Danube river walk",
              category: "nature",
              indoor_outdoor: "outdoor",
              start_time: "14:00",
              end_time: "16:00",
              was_rescheduled: true,
              latitude: 47.4969,
              longitude: 19.0489,
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
          precip_prob: 10,
          conditions: "Sunny",
          weather_conflict: false,
        },
        {
          forecast_date: "2026-09-13",
          temp_min: 12,
          temp_max: 18,
          precip_prob: 80,
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
      screen.getByRole("heading", { name: /Budapest, Hungary/i }),
    ).toBeInTheDocument();
    expect(screen.getByText("92")).toBeInTheDocument(); // 0.92 -> /100
    expect(screen.getAllByText(/€842/).length).toBeGreaterThan(0);
  });

  it("shows the total cost with a per-category breakdown", () => {
    render(<ResultsPage response={fixture()} />);
    const card = screen
      .getByRole("heading", { name: "Total cost" })
      .closest("section") as HTMLElement;
    const scoped = within(card);
    expect(scoped.getByText("Transport")).toBeInTheDocument();
    expect(scoped.getByText("Accommodation")).toBeInTheDocument();
    expect(scoped.getByText("Activities")).toBeInTheDocument();
    expect(scoped.getByText(/€245/)).toBeInTheDocument();
    expect(scoped.getByText(/€320/)).toBeInTheDocument();
    expect(scoped.getByText(/€277/)).toBeInTheDocument();
    expect(scoped.getByText(/€842/)).toBeInTheDocument();
  });

  it("renders transport details with mode, distance, cost and duration", () => {
    render(<ResultsPage response={fixture()} />);
    const card = screen
      .getByRole("heading", { name: "Transport" })
      .closest("section") as HTMLElement;
    const scoped = within(card);
    expect(scoped.getByText(/640 km/)).toBeInTheDocument();
    expect(scoped.getByText(/€245/)).toBeInTheDocument();
    expect(scoped.getByText(/6 h 15 min/)).toBeInTheDocument(); // 375 min
    // The derived travel dates + departure time are shown, not just a price.
    expect(scoped.getByText(/1 Dec, 10:15/)).toBeInTheDocument(); // departure
    expect(scoped.getByText(/5 Dec/)).toBeInTheDocument(); // return
  });

  it("shows the trip date range in the header", () => {
    render(<ResultsPage response={fixture()} />);
    expect(screen.getByText("1 Dec – 5 Dec 2026")).toBeInTheDocument();
  });

  it("renders accommodation with name, nightly price, nights and rating", () => {
    render(<ResultsPage response={fixture()} />);
    const card = screen
      .getByRole("heading", { name: "Accommodation" })
      .closest("section") as HTMLElement;
    const scoped = within(card);
    expect(scoped.getByText("Riverview Boutique")).toBeInTheDocument();
    expect(scoped.getByText(/€80/)).toBeInTheDocument(); // nightly price
    // 320 accommodation spend / 80 per night = 4 nights.
    expect(scoped.getByText(/4 nights/)).toBeInTheDocument();
    expect(scoped.getByText(/★ 4.6/)).toBeInTheDocument();
    // Check-in/check-out match the trip window (1 Dec – 5 Dec).
    expect(scoped.getByText("Check-in")).toBeInTheDocument();
    expect(scoped.getByText("Check-out")).toBeInTheDocument();
    expect(scoped.getByText("1 Dec")).toBeInTheDocument();
    expect(scoped.getByText("5 Dec")).toBeInTheDocument();
  });

  it("shows accommodation listing detail and the Airbnb link", () => {
    render(<ResultsPage response={fixture()} />);
    const card = screen
      .getByRole("heading", { name: "Accommodation" })
      .closest("section") as HTMLElement;
    const scoped = within(card);
    // Neighbourhood, review count, amenities and location score come from the listing.
    expect(scoped.getByText(/District V/)).toBeInTheDocument();
    expect(scoped.getByText(/\(312 reviews\)/)).toBeInTheDocument();
    expect(scoped.getByText("Wi-Fi")).toBeInTheDocument();
    expect(scoped.getByText(/Excellent location/)).toBeInTheDocument();
    // The "View on Airbnb" button links to the real listing.
    const link = scoped.getByRole("link", { name: /View on Airbnb/i });
    expect(link).toHaveAttribute("href", "https://www.airbnb.com/rooms/12345");
  });

  it("shows the itinerary per day with indoor/outdoor and rescheduled markers", () => {
    render(<ResultsPage response={fixture()} />);
    expect(screen.getByRole("heading", { name: "Day 1" })).toBeInTheDocument();
    expect(screen.getByRole("heading", { name: "Day 2" })).toBeInTheDocument();
    // Indoor/outdoor tags.
    expect(screen.getByText("Indoor")).toBeInTheDocument();
    expect(screen.getByText("Outdoor")).toBeInTheDocument();
    // The rescheduled activity carries a marker.
    expect(screen.getByText(/rescheduled/i)).toBeInTheDocument();
    // An empty day is shown as free.
    expect(screen.getByText(/free day/i)).toBeInTheDocument();
  });

  it("renders a weather forecast with temperatures and precipitation", () => {
    render(<ResultsPage response={fixture()} />);
    const card = screen
      .getByRole("heading", { name: /Weather · 2-day forecast/i })
      .closest("section") as HTMLElement;
    const scoped = within(card);
    // Each day shows its high and low temperature separately.
    expect(scoped.getByText("24°")).toBeInTheDocument(); // day 1 high
    expect(scoped.getByText("18°")).toBeInTheDocument(); // day 2 high
    expect(scoped.getByText("12°")).toBeInTheDocument(); // day 2 low
    // The rainy day surfaces its precipitation chance (percentage, not *100).
    expect(scoped.getByText("80%")).toBeInTheDocument();
  });

  it("renders the explanation as 'Why {city}?'", () => {
    render(<ResultsPage response={fixture()} />);
    expect(screen.getByText(/why budapest\?/i)).toBeInTheDocument();
    expect(
      screen.getByText(/best value among the candidates/i),
    ).toBeInTheDocument();
  });

  it("calls onBack from the header and the primary action", () => {
    const onBack = vi.fn();
    render(<ResultsPage response={fixture()} onBack={onBack} />);
    fireEvent.click(screen.getByRole("button", { name: /back to planning/i }));
    fireEvent.click(screen.getByRole("button", { name: /plan another trip/i }));
    expect(onBack).toHaveBeenCalledTimes(2);
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

  it("shows a fallback explanation when none is provided", () => {
    const r = fixture();
    r.explanation = null;
    render(<ResultsPage response={r} />);
    // The section still renders (heading present) with a generic rationale.
    expect(screen.getByText(/why budapest\?/i)).toBeInTheDocument();
    expect(screen.getByText(/scored highest/i)).toBeInTheDocument();
  });

  it("maps the destination, accommodation and located activities in the legend", () => {
    render(<ResultsPage response={fixture()} />);
    const card = screen
      .getByRole("heading", { name: "Trip map" })
      .closest("section") as HTMLElement;
    const scoped = within(card);
    expect(scoped.getByText("City centre")).toBeInTheDocument();
    expect(scoped.getByText("Accommodation")).toBeInTheDocument();
    // Activities carry POI coordinates in the fixture, so they appear in the legend.
    expect(scoped.getByText("Activities")).toBeInTheDocument();
  });

  it("omits activities from the map legend when they have no coordinates", () => {
    const r = fixture();
    for (const day of r.chosen_plan.itinerary) {
      for (const activity of day.activities) {
        activity.latitude = null;
        activity.longitude = null;
      }
    }
    render(<ResultsPage response={r} />);
    const card = screen
      .getByRole("heading", { name: "Trip map" })
      .closest("section") as HTMLElement;
    expect(within(card).queryByText("Activities")).not.toBeInTheDocument();
  });

  it("labels transport and accommodation with their data-quality flag", () => {
    render(<ResultsPage response={fixture()} />);
    // transport quality_flag: "real" -> "Live"; accommodation: "cached" -> "Cached".
    expect(screen.getByText("Live")).toBeInTheDocument();
    expect(screen.getByText("Cached")).toBeInTheDocument();
  });

  it("surfaces degraded sources with a readable notice", () => {
    const r = fixture();
    r.meta.degraded_sources = ["engine:stub", "transport:estimated"];
    render(<ResultsPage response={r} />);
    const notice = screen.getByLabelText("Data quality notice");
    const scoped = within(notice);
    expect(scoped.getByText(/estimated or cached/i)).toBeInTheDocument();
    expect(scoped.getByText(/placeholder data/i)).toBeInTheDocument(); // engine:stub
    expect(scoped.getByText(/Transport — estimated/i)).toBeInTheDocument();
  });

  it("does not render the data-quality notice when no sources are degraded", () => {
    render(<ResultsPage response={fixture()} />); // fixture has degraded_sources: []
    expect(
      screen.queryByLabelText("Data quality notice"),
    ).not.toBeInTheDocument();
  });
});
