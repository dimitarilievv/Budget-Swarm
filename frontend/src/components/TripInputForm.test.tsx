import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { vi } from "vitest";

import { ApiError } from "../api/errors";
import type { TripRequestPayload, TripResponse } from "../api/types";
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

describe("TripInputForm", () => {
  it("renders all documented fields", () => {
    render(<TripInputForm />);
    expect(screen.getByLabelText(/starting from/i)).toBeInTheDocument();
    expect(screen.getByLabelText(/budget/i)).toBeInTheDocument();
    expect(screen.getByLabelText(/trip length/i)).toBeInTheDocument();
    expect(screen.getByLabelText(/travelers/i)).toBeInTheDocument();
    expect(screen.getByLabelText(/transport/i)).toBeInTheDocument();
    // Weighted preference controls.
    expect(screen.getByLabelText(/culture/i)).toBeInTheDocument();
    expect(screen.getByLabelText(/food/i)).toBeInTheDocument();
  });

  it("shows vehicle_consumption only for car", () => {
    render(<TripInputForm />);
    // Default is car -> fuel field present.
    expect(screen.getByLabelText(/fuel use/i)).toBeInTheDocument();
    // Switch to train -> fuel field gone.
    fireEvent.change(screen.getByLabelText(/transport/i), {
      target: { value: "train" },
    });
    expect(screen.queryByLabelText(/fuel use/i)).not.toBeInTheDocument();
    // Back to car -> it returns.
    fireEvent.change(screen.getByLabelText(/transport/i), {
      target: { value: "car" },
    });
    expect(screen.getByLabelText(/fuel use/i)).toBeInTheDocument();
  });

  it("blocks an invalid submit client-side (does not call the API)", async () => {
    const submit = vi.fn();
    render(<TripInputForm submit={submit} />);
    setOrigin();
    fireEvent.change(screen.getByLabelText(/budget/i), {
      target: { value: "0" },
    });
    fireEvent.click(screen.getByRole("button", { name: /plan my trip/i }));

    expect(
      await screen.findByText(/budget must be greater than 0/i),
    ).toBeInTheDocument();
    expect(submit).not.toHaveBeenCalled();
  });

  it("requires fuel use for car and blocks submit when missing", async () => {
    const submit = vi.fn();
    render(<TripInputForm submit={submit} />);
    setOrigin();
    fireEvent.change(screen.getByLabelText(/fuel use/i), {
      target: { value: "" },
    });
    fireEvent.click(screen.getByRole("button", { name: /plan my trip/i }));

    expect(
      await screen.findByText(/fuel use .* is required/i),
    ).toBeInTheDocument();
    expect(submit).not.toHaveBeenCalled();
  });

  it("blocks submit when no preference is selected", async () => {
    const submit = vi.fn();
    render(<TripInputForm submit={submit} />);
    setOrigin();
    for (const theme of ["culture", "food"]) {
      fireEvent.change(screen.getByLabelText(new RegExp(theme, "i")), {
        target: { value: "0" },
      });
    }
    fireEvent.click(screen.getByRole("button", { name: /plan my trip/i }));

    expect(
      await screen.findByText(/pick at least one preference/i),
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
    fireEvent.click(screen.getByRole("button", { name: /plan my trip/i }));

    await waitFor(() => expect(submit).toHaveBeenCalledTimes(1));
    const payload = submit.mock.calls[0][0];
    expect(payload.budget).toBe(1000);
    expect(payload.transport_mode).toBe("car");
    expect(payload.vehicle_consumption).toBe(6.5);
    expect(payload.origin_label).toBe("Skopje");
    expect(payload.preferences).toMatchObject({ culture: 0.8, food: 0.6 });
    expect(payload.preferences).not.toHaveProperty("nightlife"); // zero-weight dropped

    expect(await screen.findByText(/Budapest, Hungary/i)).toBeInTheDocument();
    expect(onResult).toHaveBeenCalledTimes(1);
  });

  it("surfaces an API error envelope to the user", async () => {
    const submit = vi.fn(async () => {
      throw new ApiError(
        "NO_PLAN_AVAILABLE",
        "No feasible plan within your budget.",
      );
    });
    render(<TripInputForm submit={submit} />);
    setOrigin("Skopje");
    fireEvent.click(screen.getByRole("button", { name: /plan my trip/i }));

    expect(
      await screen.findByText(/no feasible plan within your budget/i),
    ).toBeInTheDocument();
  });
});
