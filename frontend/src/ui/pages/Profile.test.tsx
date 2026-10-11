import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

import ProfilePage from "./Profile";

const changePassword = vi.fn(async () => {});
const deleteAccount = vi.fn(async () => {});
const updateProfile = vi.fn(async () => {});
const logout = vi.fn();
const navigate = vi.fn();

vi.mock("@/router", () => ({
  navigate: (to: string) => navigate(to),
}));

vi.mock("@/api/endpoints", () => ({
  listTrips: vi.fn(async () => []),
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
    logout,
    changePassword,
    deleteAccount,
    updateProfile,
  }),
}));

function typeInto(label: RegExp, value: string) {
  fireEvent.change(screen.getByLabelText(label), { target: { value } });
}

function openSecurity() {
  fireEvent.click(screen.getByRole("button", { name: /security/i }));
}

function openDanger() {
  fireEvent.click(screen.getByRole("button", { name: /remove your data/i }));
}

beforeEach(() => {
  changePassword.mockClear();
  deleteAccount.mockClear();
  updateProfile.mockClear();
  navigate.mockClear();
});

describe("ProfilePage", () => {
  it("shows the account details on the default tab", () => {
    render(<ProfilePage />);
    expect(screen.getByDisplayValue("Ana")).toBeInTheDocument(); // name input
    expect(screen.getByDisplayValue("ana@example.com")).toBeInTheDocument(); // email (read-only)
  });

  it("saves a changed display name", async () => {
    render(<ProfilePage />);
    typeInto(/full name/i, "Ana Smith");
    fireEvent.click(screen.getByRole("button", { name: /save changes/i }));
    await waitFor(() =>
      expect(updateProfile).toHaveBeenCalledWith("Ana Smith"),
    );
  });

  it("keeps the password button disabled until valid and matching", () => {
    render(<ProfilePage />);
    openSecurity();
    typeInto(/current password/i, "password123");
    typeInto(/^new password/i, "newpassword456");
    typeInto(/confirm new password/i, "different999");

    const btn = screen.getByRole("button", { name: /update password/i });
    expect(btn).toBeDisabled();
    expect(screen.getByText(/doesn.t match yet/i)).toBeInTheDocument();
    expect(changePassword).not.toHaveBeenCalled();
  });

  it("submits a valid password change and reports success", async () => {
    render(<ProfilePage />);
    openSecurity();
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
    openSecurity();
    typeInto(/current password/i, "wrongpassword");
    typeInto(/^new password/i, "newpassword456");
    typeInto(/confirm new password/i, "newpassword456");
    fireEvent.click(screen.getByRole("button", { name: /update password/i }));

    expect(
      await screen.findByText(/current password is incorrect/i),
    ).toBeInTheDocument();
  });

  it("deletes the account only after typing DELETE, then returns home", async () => {
    render(<ProfilePage />);
    openDanger();

    const confirmBtn = screen.getByRole("button", {
      name: /^delete account$/i,
    });
    expect(confirmBtn).toBeDisabled();

    fireEvent.change(screen.getByPlaceholderText("DELETE"), {
      target: { value: "DELETE" },
    });
    expect(confirmBtn).toBeEnabled();

    fireEvent.click(confirmBtn);
    await waitFor(() => expect(deleteAccount).toHaveBeenCalledTimes(1));
    await waitFor(() => expect(navigate).toHaveBeenCalledWith("/"));
  });
});
