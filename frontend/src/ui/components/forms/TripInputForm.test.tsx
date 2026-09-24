import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { vi } from "vitest";

import { ApiError } from "@/api/errors";
import type { TripRequestPayload, TripResponse } from "@/types/trip";
import TripInputForm from "./TripInputForm";

function fakeResponse(city = "Budapest"): TripResponse {
  return {
    chosen_plan: {
      destination: {
        id: 1,
        city,
        country: "Hungary",
        latitude: 47.5,
        longitude: 19,
      },
      overall_score: 0.87,
      total_cost: 812,
      cost_breakdown: { transport: 151, accommodation: 240, activities: 421 },
      transport: null,
      accommodation: null,
      itinerary: [],
      weather: [],
    },
    ranked_candidates: [],
    explanation: "It fits your budget.",
    meta: { generated_at: "2026-01-01T00:00:00Z", degraded_sources: [] },
  };
}

/** Fill origin so a valid submit is possible (origin is required). */
function setOrigin(label = "Skopje") {
  fireEvent.change(screen.getByLabelText(/starting from/i), {
    target: { value: label },
  });
}

const plan = () =>
  screen.getByRole("button", { name: /generate optimized plan/i });

describe("TripInputForm", () => {
  it("renders all documented fields", () => {
    render(<TripInputForm />);
    expect(screen.getByLabelText(/starting from/i)).toBeInTheDocument();
    expect(screen.getByLabelText(/budget/i)).toBeInTheDocument();
    expect(screen.getByLabelText(/trip duration/i)).toBeInTheDocument();
    expect(screen.getByLabelText("Travelers")).toBeInTheDocument();
    // Transport as segmented buttons.
    expect(
      screen.getByRole("button", { name: /airplane/i }),
    ).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /car/i })).toBeInTheDocument();
    // Interests as chips.
    expect(
      screen.getByRole("button", { name: /culture/i }),
    ).toBeInTheDocument();
    expect(
      screen.getByRole("button", { name: /sightseeing/i }),
    ).toBeInTheDocument();
  });

  it("shows vehicle_consumption only for car", () => {
    render(<TripInputForm />);
    // Default is car -> field present.
    expect(screen.getByLabelText(/vehicle consumption/i)).toBeInTheDocument();
    // Switch to train -> gone.
    fireEvent.click(screen.getByRole("button", { name: /train/i }));
    expect(
      screen.queryByLabelText(/vehicle consumption/i),
    ).not.toBeInTheDocument();
    // Back to car -> returns.
    fireEvent.click(screen.getByRole("button", { name: /car/i }));
    expect(screen.getByLabelText(/vehicle consumption/i)).toBeInTheDocument();
  });

  it("blocks an invalid submit client-side (does not call the API)", async () => {
    const submit = vi.fn();
    render(<TripInputForm submit={submit} />);
    setOrigin();
    fireEvent.change(screen.getByLabelText(/budget/i), {
      target: { value: "0" },
    });
    fireEvent.click(plan());

    expect(
      await screen.findByText(/budget must be greater than 0/i),
    ).toBeInTheDocument();
    expect(submit).not.toHaveBeenCalled();
  });

  it("requires vehicle consumption for car and blocks submit when missing", async () => {
    const submit = vi.fn();
    render(<TripInputForm submit={submit} />);
    setOrigin();
    fireEvent.change(screen.getByLabelText(/vehicle consumption/i), {
      target: { value: "" },
    });
    fireEvent.click(plan());

    expect(await screen.findByText(/required for car/i)).toBeInTheDocument();
    expect(submit).not.toHaveBeenCalled();
  });

  it("blocks submit when no interest is selected", async () => {
    const submit = vi.fn();
    render(<TripInputForm submit={submit} />);
    setOrigin();
    // Deselect the three default-active chips.
    for (const name of ["culture", "food", "sightseeing"]) {
      fireEvent.click(
        screen.getByRole("button", { name: new RegExp(name, "i") }),
      );
    }
    fireEvent.click(plan());

    expect(
      await screen.findByText(/pick at least one interest/i),
    ).toBeInTheDocument();
    expect(submit).not.toHaveBeenCalled();
  });

  it("submits a valid payload and shows the plan", async () => {
    const submit = vi.fn<(p: TripRequestPayload) => Promise<TripResponse>>(
      async () => fakeResponse("Budapest"),
    );
    const onResult = vi.fn();
    render(<TripInputForm submit={submit} onResult={onResult} />);
    setOrigin("Skopje");
    fireEvent.click(plan());

    await waitFor(() => expect(submit).toHaveBeenCalledTimes(1));
    const payload = submit.mock.calls[0][0];
    expect(payload.budget).toBe(1000);
    expect(payload.transport_mode).toBe("car");
    expect(payload.vehicle_consumption).toBe(6.5);
    expect(payload.origin_label).toBe("Skopje");
    // Default interests -> equal weights; unselected themes are absent.
    expect(payload.preferences).toMatchObject({
      culture: 1,
      food: 1,
      tourism: 1,
    });
    expect(payload.preferences).not.toHaveProperty("nightlife");

    // The plan is handed up to the parent (App renders the Results page from it).
    await waitFor(() => expect(onResult).toHaveBeenCalledTimes(1));
    expect(onResult.mock.calls[0][0].chosen_plan.destination.city).toBe(
      "Budapest",
    );
  });

  it("shows the loading screen while the request is in flight", async () => {
    // A request that never resolves keeps the form in the loading state.
    const submit = vi.fn(() => new Promise<TripResponse>(() => {}));
    render(<TripInputForm submit={submit} />);
    setOrigin("Skopje");
    fireEvent.click(plan());

    expect(await screen.findByText(/agents are on it/i)).toBeInTheDocument();
  });

  it("shows a dedicated no-trip state and returns to the form to adjust", async () => {
    const submit = vi.fn(async () => {
      throw new ApiError(
        "NO_PLAN_AVAILABLE",
        "No feasible plan within your budget.",
      );
    });
    render(<TripInputForm submit={submit} />);
    setOrigin("Skopje");
    fireEvent.click(plan());

    // Dedicated empty state: friendly title + the server's own message.
    expect(
      await screen.findByText(/no trip fits these constraints/i),
    ).toBeInTheDocument();
    expect(
      screen.getByText(/no feasible plan within your budget/i),
    ).toBeInTheDocument();

    // "Adjust your budget or preferences" returns to the form (no pointless re-submit).
    fireEvent.click(
      screen.getByRole("button", {
        name: /adjust your budget or preferences/i,
      }),
    );
    expect(
      screen.getByRole("button", { name: /generate optimized plan/i }),
    ).toBeInTheDocument();
    expect(submit).toHaveBeenCalledTimes(1);
  });

  it("edit details returns to the form", async () => {
    const submit = vi.fn(async () => {
      throw new ApiError("INTERNAL_ERROR", "boom");
    });
    render(<TripInputForm submit={submit} />);
    setOrigin("Skopje");
    fireEvent.click(plan());

    fireEvent.click(
      await screen.findByRole("button", { name: /edit details/i }),
    );
    expect(
      screen.getByRole("button", { name: /generate optimized plan/i }),
    ).toBeInTheDocument();
  });
});
