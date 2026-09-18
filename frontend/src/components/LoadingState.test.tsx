import { render, screen } from "@testing-library/react";

import LoadingState from "./LoadingState";

describe("LoadingState", () => {
  it("shows the agent-progress screen with every stage", () => {
    render(<LoadingState />);
    expect(screen.getByText(/agents are on it/i)).toBeInTheDocument();
    expect(screen.getByText(/scouting destinations/i)).toBeInTheDocument();
    expect(screen.getByText(/comparing transport/i)).toBeInTheDocument();
    expect(screen.getByText(/finding places to stay/i)).toBeInTheDocument();
    expect(screen.getByText(/checking the weather/i)).toBeInTheDocument();
    expect(screen.getByText(/optimizing your trip/i)).toBeInTheDocument();
    expect(screen.getByRole("progressbar")).toBeInTheDocument();
    expect(screen.getByText(/tip:/i)).toBeInTheDocument();
  });
});
