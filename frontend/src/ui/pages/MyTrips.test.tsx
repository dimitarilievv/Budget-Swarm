import { fireEvent, render, screen, within } from "@testing-library/react";
import { beforeEach, vi } from "vitest";

import type { SavedTrip } from "@/api/endpoints";

// Mock the API, auth and trip-context dependencies so the page renders standalone.
const listTrips = vi.fn();
const deleteTrip = vi.fn(async (id: number) => id);
const getTrip = vi.fn();
const updateTrip = vi.fn();

vi.mock("@/api/endpoints", () => ({
  listTrips: () => listTrips(),
  deleteTrip: (id: number) => deleteTrip(id),
  getTrip: (id: number) => getTrip(id),
  updateTrip: (id: number, p: unknown) => updateTrip(id, p),
}));

vi.mock("@/contexts/AuthContext", () => ({
  useAuth: () => ({
    status: "authenticated",
    user: { id: 1, name: "Ana", email: "ana@example.com" },
    login: vi.fn(),
    register: vi.fn(),
    logout: vi.fn(),
  }),
}));

vi.mock("@/hooks/useTripResult", () => ({
  useTripResult: () => ({
    result: null,
    request: null,
    setResult: vi.fn(),
    clear: vi.fn(),
  }),
}));

import MyTripsPage from "@/ui/pages/MyTrips";

function trip(over: Partial<SavedTrip> = {}): SavedTrip {
  return {
    id: 1,
    title: null,
    notes: null,
    origin_label: "Skopje",
    destination_city: "Istanbul",
    destination_country: "Turkey",
    transport_mode: "airplane",
    duration_days: 5,
    travelers: 2,
    budget: 1000,
    total_cost: 329,
    overall_score: 0.9,
    created_at: "2026-01-01T00:00:00Z",
    ...over,
  };
}

beforeEach(() => {
  vi.clearAllMocks();
  vi.useRealTimers();
});

describe("MyTripsPage", () => {
  it("renders saved trips as tickets with the count", async () => {
    listTrips.mockResolvedValue([
      trip({
        id: 1,
        destination_city: "Istanbul",
        destination_country: "Turkey",
      }),
      trip({
        id: 2,
        destination_city: "Prague",
        destination_country: "Czechia",
      }),
    ]);
    render(<MyTripsPage />);
    expect(
      screen.getByRole("heading", { name: "My trips" }),
    ).toBeInTheDocument();
    expect(await screen.findByText("Istanbul, Turkey")).toBeInTheDocument();
    expect(screen.getByText("Prague, Czechia")).toBeInTheDocument();
  });

  it("filters trips by the search box", async () => {
    listTrips.mockResolvedValue([
      trip({
        id: 1,
        destination_city: "Istanbul",
        destination_country: "Turkey",
      }),
      trip({
        id: 2,
        destination_city: "Prague",
        destination_country: "Czechia",
      }),
    ]);
    render(<MyTripsPage />);
    await screen.findByText("Istanbul, Turkey");
    fireEvent.change(screen.getByLabelText("Search trips"), {
      target: { value: "prague" },
    });
    expect(screen.queryByText("Istanbul, Turkey")).not.toBeInTheDocument();
    expect(screen.getByText("Prague, Czechia")).toBeInTheDocument();
  });

  it("shows the empty state when there are no saved trips", async () => {
    listTrips.mockResolvedValue([]);
    render(<MyTripsPage />);
    expect(await screen.findByText(/no saved trips yet/i)).toBeInTheDocument();
  });

  it("deletes a trip optimistically and can undo", async () => {
    listTrips.mockResolvedValue([
      trip({ id: 1, destination_city: "Istanbul" }),
    ]);
    render(<MyTripsPage />);
    await screen.findByText(/Istanbul/);

    fireEvent.click(screen.getByRole("button", { name: /trip actions/i }));
    fireEvent.click(screen.getByRole("menuitem", { name: /delete trip/i }));

    // Toast appears; the card is gone from the list.
    const toast = await screen.findByRole("status");
    expect(within(toast).getByText(/Istanbul deleted/i)).toBeInTheDocument();

    fireEvent.click(within(toast).getByRole("button", { name: /undo/i }));
    expect(await screen.findByText(/Istanbul/)).toBeInTheDocument();
    expect(deleteTrip).not.toHaveBeenCalled(); // undone before the server delete
  });
});
