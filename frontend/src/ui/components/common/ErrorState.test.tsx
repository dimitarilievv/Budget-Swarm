import { fireEvent, render, screen } from "@testing-library/react";
import { vi } from "vitest";

import { ApiError } from "@/api/errors";
import ErrorState from "./ErrorState";

describe("ErrorState", () => {
  it.each([
    ["VALIDATION_ERROR", /check your details/i],
    ["RATE_LIMITED", /too many requests/i],
    ["NO_PLAN_AVAILABLE", /no trip fits/i],
    ["INTERNAL_ERROR", /our side/i],
    ["NETWORK_ERROR", /can't reach the server/i],
  ])(
    "shows a clear message for %s and echoes the server message",
    (code, matcher) => {
      render(<ErrorState error={new ApiError(code, "server said so")} />);
      expect(screen.getByText(matcher)).toBeInTheDocument();
      expect(screen.getByText(/server said so/i)).toBeInTheDocument();
    },
  );

  it("shows the offending field when present", () => {
    render(
      <ErrorState
        error={new ApiError("VALIDATION_ERROR", "bad", { field: "budget" })}
      />,
    );
    expect(screen.getByText(/field: budget/i)).toBeInTheDocument();
  });

  it("falls back to a generic message for an unknown code", () => {
    render(<ErrorState error={new ApiError("WAT", "boom")} />);
    expect(screen.getByText(/something went wrong/i)).toBeInTheDocument();
  });

  it("invokes onRetry and onEdit", () => {
    const onRetry = vi.fn();
    const onEdit = vi.fn();
    render(
      <ErrorState
        error={new ApiError("INTERNAL_ERROR", "x")}
        onRetry={onRetry}
        onEdit={onEdit}
      />,
    );
    fireEvent.click(screen.getByRole("button", { name: /try again/i }));
    fireEvent.click(screen.getByRole("button", { name: /edit details/i }));
    expect(onRetry).toHaveBeenCalledTimes(1);
    expect(onEdit).toHaveBeenCalledTimes(1);
  });
});
