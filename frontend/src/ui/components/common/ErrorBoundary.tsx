/**
 * ErrorBoundary — a top-level safety net for render-time crashes.
 *
 * React unmounts the whole tree if a render throws; without a boundary that means a blank white
 * screen. This catches those errors, logs them, and shows a branded recovery card with a reload
 * so a single bad render never leaves the user stranded. It only catches render/lifecycle errors
 * (not event handlers or async code — those are handled where they occur, e.g. ApiError).
 */
import { Component, type ErrorInfo, type ReactNode } from "react";

interface ErrorBoundaryProps {
  children: ReactNode;
}

interface ErrorBoundaryState {
  hasError: boolean;
}

export default class ErrorBoundary extends Component<
  ErrorBoundaryProps,
  ErrorBoundaryState
> {
  state: ErrorBoundaryState = { hasError: false };

  static getDerivedStateFromError(): ErrorBoundaryState {
    return { hasError: true };
  }

  componentDidCatch(error: Error, info: ErrorInfo): void {
    // Surface the crash for debugging; a real deployment would forward this to a logger.
    console.error("Unhandled UI error:", error, info.componentStack);
  }

  private handleReload = (): void => {
    // Full reload is the safest recovery from an unknown render fault.
    window.location.reload();
  };

  render(): ReactNode {
    if (!this.state.hasError) return this.props.children;

    return (
      <div
        role="alert"
        className="flex min-h-screen flex-col items-center justify-center gap-4 bg-[#eef7f5] px-6 text-center"
      >
        <span className="text-4xl" aria-hidden>
          🧭
        </span>
        <h1 className="text-2xl font-bold text-slate-800">
          Something went off course
        </h1>
        <p className="max-w-md text-slate-600">
          An unexpected error interrupted the page. Reloading usually clears it
          — your details are safe.
        </p>
        <button
          type="button"
          onClick={this.handleReload}
          className="rounded-xl bg-teal-600 px-5 py-2.5 font-semibold text-white shadow-sm transition hover:bg-teal-700"
        >
          Reload the app
        </button>
      </div>
    );
  }
}
