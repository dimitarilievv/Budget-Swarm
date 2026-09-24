import { fireEvent, render, screen } from "@testing-library/react";
import { vi } from "vitest";

import NoTripFound from "./NoTripFound";

describe("NoTripFound", () => {
  it("shows the heading and echoes the budget", () => {
    render(<NoTripFound budget={1000} />);
    expect(
      screen.getByText(/no trip fits these constraints/i),
    ).toBeInTheDocument();
    expect(screen.getByText(/€1,?000/)).toBeInTheDocument();
  });

  it("renders the server message when provided", () => {
    render(<NoTripFound budget={800} message="No feasible plan found." />);
    expect(screen.getByText(/no feasible plan found/i)).toBeInTheDocument();
  });

  it("invokes onAdjust from both actions", () => {
    const onAdjust = vi.fn();
    render(<NoTripFound budget={1000} onAdjust={onAdjust} />);
    fireEvent.click(
      screen.getByRole("button", {
        name: /adjust your budget or preferences/i,
      }),
    );
    fireEvent.click(
      screen.getByRole("button", { name: /try different preferences/i }),
    );
    expect(onAdjust).toHaveBeenCalledTimes(2);
  });

  it("omits the budget clause when no budget is given", () => {
    render(<NoTripFound />);
    expect(screen.queryByText(/within your/i)).not.toBeInTheDocument();
  });
});
