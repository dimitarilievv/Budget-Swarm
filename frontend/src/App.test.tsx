import { render, screen } from "@testing-library/react";

import App from "./App";

describe("App", () => {
  it("renders the hero headline and the brand", () => {
    render(<App />);
    expect(
      screen.getByRole("heading", { name: /tell us your budget/i }),
    ).toBeInTheDocument();
    expect(screen.getByText(/AI Travel Planner/i)).toBeInTheDocument();
  });

  it("renders the trip input form's CTA", () => {
    render(<App />);
    expect(
      screen.getByRole("button", { name: /plan my trip/i }),
    ).toBeInTheDocument();
  });
});
