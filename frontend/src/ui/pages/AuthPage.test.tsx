import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { vi } from "vitest";

import AuthPage from "@/ui/pages/AuthPage";

describe("AuthPage", () => {
  it("renders the login mode by default", () => {
    render(<AuthPage initialMode="login" />);
    expect(
      screen.getByRole("heading", { name: "Welcome back" }),
    ).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Log in" })).toBeInTheDocument();
    // No name field in login mode.
    expect(screen.queryByLabelText("Full name")).not.toBeInTheDocument();
  });

  it("switching to Sign up reveals the name field and reports the mode change", () => {
    const onModeChange = vi.fn();
    render(<AuthPage initialMode="login" onModeChange={onModeChange} />);
    fireEvent.click(screen.getByRole("tab", { name: "Sign up" }));
    expect(onModeChange).toHaveBeenCalledWith("register");
    expect(
      screen.getByRole("heading", { name: "Create your account" }),
    ).toBeInTheDocument();
    expect(screen.getByLabelText("Full name")).toBeInTheDocument();
  });

  it("validates the email before calling onLogin", async () => {
    const onLogin = vi.fn();
    render(<AuthPage initialMode="login" onLogin={onLogin} />);
    fireEvent.change(screen.getByLabelText("Email"), {
      target: { value: "nope" },
    });
    fireEvent.change(screen.getByPlaceholderText("Your password"), {
      target: { value: "secret12" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Log in" }));
    expect(
      await screen.findByText("Enter a valid email address"),
    ).toBeInTheDocument();
    expect(onLogin).not.toHaveBeenCalled();
  });

  it("submits valid login credentials with the remember flag", async () => {
    const onLogin = vi.fn().mockResolvedValue(undefined);
    render(<AuthPage initialMode="login" onLogin={onLogin} />);
    fireEvent.change(screen.getByLabelText("Email"), {
      target: { value: "user@example.com" },
    });
    fireEvent.change(screen.getByPlaceholderText("Your password"), {
      target: { value: "secret12" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Log in" }));
    await waitFor(() =>
      expect(onLogin).toHaveBeenCalledWith({
        email: "user@example.com",
        password: "secret12",
        remember: true,
      }),
    );
  });

  it("requires accepting the terms before registering", async () => {
    const onRegister = vi.fn();
    render(<AuthPage initialMode="register" onRegister={onRegister} />);
    fireEvent.change(screen.getByLabelText("Full name"), {
      target: { value: "Ana" },
    });
    fireEvent.change(screen.getByLabelText("Email"), {
      target: { value: "ana@example.com" },
    });
    fireEvent.change(screen.getByPlaceholderText("At least 8 characters"), {
      target: { value: "password123" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Create account" }));
    expect(
      await screen.findByText("Please accept the terms to continue"),
    ).toBeInTheDocument();
    expect(onRegister).not.toHaveBeenCalled();
  });

  it("surfaces a server error returned by onLogin", async () => {
    const onLogin = vi
      .fn()
      .mockRejectedValue(new Error("Invalid email or password"));
    render(<AuthPage initialMode="login" onLogin={onLogin} />);
    fireEvent.change(screen.getByLabelText("Email"), {
      target: { value: "user@example.com" },
    });
    fireEvent.change(screen.getByPlaceholderText("Your password"), {
      target: { value: "secret12" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Log in" }));
    expect(await screen.findByRole("alert")).toHaveTextContent(
      "Invalid email or password",
    );
  });
});
