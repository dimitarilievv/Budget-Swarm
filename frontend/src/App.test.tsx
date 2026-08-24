import { render, screen } from "@testing-library/react";
import App from "./App";

describe("App", () => {
  it("renders the landing heading (smoke test)", () => {
    render(<App />);
    expect(
      screen.getByRole("heading", { name: /budgetswarm/i }),
    ).toBeInTheDocument();
  });
});
