import { fireEvent, render, screen } from "@testing-library/react";
import { vi } from "vitest";

import AccountChip from "./AccountChip";

const navigate = vi.fn();
vi.mock("@/router", () => ({ navigate: (to: string) => navigate(to) }));

beforeEach(() => navigate.mockClear());

describe("AccountChip", () => {
  it("shows the name and initials, and opens the profile on click", () => {
    render(<AccountChip name="Ada Lovelace" email="ada@example.com" />);
    expect(screen.getByText("Ada Lovelace")).toBeInTheDocument();
    expect(screen.getByText("AL")).toBeInTheDocument(); // two-initial avatar
    fireEvent.click(screen.getByRole("button", { name: /your account/i }));
    expect(navigate).toHaveBeenCalledWith("/profile");
  });

  it("falls back to the email and its first initial when there is no name", () => {
    render(<AccountChip name={null} email="zoe@example.com" />);
    expect(screen.getByText("zoe@example.com")).toBeInTheDocument();
    expect(screen.getByText("Z")).toBeInTheDocument();
  });
});
