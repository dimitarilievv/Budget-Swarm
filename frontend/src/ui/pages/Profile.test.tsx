import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { vi } from "vitest";

import ProfilePage from "./Profile";

const changePassword = vi.fn(async () => {});
const deleteAccount = vi.fn(async () => {});
const navigate = vi.fn();

vi.mock("@/router", () => ({
  navigate: (to: string) => navigate(to),
}));

vi.mock("@/contexts/AuthContext", () => ({
  useAuth: () => ({
    status: "authenticated",
    user: {
      id: 1,
      name: "Ana",
      email: "ana@example.com",
      created_at: "2026-01-15T00:00:00Z",
      updated_at: "2026-01-15T00:00:00Z",
    },
    changePassword,
    deleteAccount,
  }),
}));

function typeInto(label: RegExp, value: string) {
  fireEvent.change(screen.getByLabelText(label), { target: { value } });
}

beforeEach(() => {
  changePassword.mockClear();
  deleteAccount.mockClear();
  navigate.mockClear();
});

describe("ProfilePage", () => {
  it("shows the account info", () => {
    render(<ProfilePage />);
    expect(screen.getByText("ana@example.com")).toBeInTheDocument();
    expect(screen.getByText("Ana")).toBeInTheDocument();
  });

  it("blocks a mismatched confirmation and does not call the API", async () => {
    render(<ProfilePage />);
    typeInto(/current password/i, "password123");
    typeInto(/^new password/i, "newpassword456");
    typeInto(/confirm new password/i, "different999");
    fireEvent.click(screen.getByRole("button", { name: /update password/i }));

    expect(await screen.findByText(/do not match/i)).toBeInTheDocument();
    expect(changePassword).not.toHaveBeenCalled();
  });

  it("blocks a too-short new password", async () => {
    render(<ProfilePage />);
    typeInto(/current password/i, "password123");
    typeInto(/^new password/i, "short");
    typeInto(/confirm new password/i, "short");
    fireEvent.click(screen.getByRole("button", { name: /update password/i }));

    expect(
      await screen.findByText(/new password must be at least 8 characters/i),
    ).toBeInTheDocument();
    expect(changePassword).not.toHaveBeenCalled();
  });

  it("submits a valid password change and reports success", async () => {
    render(<ProfilePage />);
    typeInto(/current password/i, "password123");
    typeInto(/^new password/i, "newpassword456");
    typeInto(/confirm new password/i, "newpassword456");
    fireEvent.click(screen.getByRole("button", { name: /update password/i }));

    await waitFor(() =>
      expect(changePassword).toHaveBeenCalledWith(
        "password123",
        "newpassword456",
      ),
    );
    expect(await screen.findByText(/password updated/i)).toBeInTheDocument();
  });

  it("surfaces a server error from the change-password call", async () => {
    const { ApiError } = await import("@/api/errors");
    changePassword.mockRejectedValueOnce(
      new ApiError("UNAUTHORIZED", "Current password is incorrect"),
    );
    render(<ProfilePage />);
    typeInto(/current password/i, "wrongpassword");
    typeInto(/^new password/i, "newpassword456");
    typeInto(/confirm new password/i, "newpassword456");
    fireEvent.click(screen.getByRole("button", { name: /update password/i }));

    expect(
      await screen.findByText(/current password is incorrect/i),
    ).toBeInTheDocument();
  });

  it("deletes the account only after the email is confirmed, then returns home", async () => {
    render(<ProfilePage />);
    fireEvent.click(screen.getByRole("button", { name: /^delete account$/i }));

    const confirmBtn = screen.getByRole("button", {
      name: /delete permanently/i,
    });
    expect(confirmBtn).toBeDisabled(); // armed only once the email matches

    typeInto(/type your email/i, "ana@example.com");
    expect(confirmBtn).toBeEnabled();

    fireEvent.click(confirmBtn);
    await waitFor(() => expect(deleteAccount).toHaveBeenCalledTimes(1));
    await waitFor(() => expect(navigate).toHaveBeenCalledWith("/"));
  });
});
