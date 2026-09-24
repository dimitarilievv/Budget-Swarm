/**
 * ErrorState — renders a server error envelope clearly (BS-086).
 *
 * Maps each api.md error code (422 / 429 / 503 / 500, plus the client NETWORK_ERROR) to a
 * friendly title + hint, shows the server's own message as detail, and offers a retry
 * affordance. Announced via role="alert".
 */
import type { ApiError } from "@/api/errors";

const MESSAGES: Record<string, { title: string; hint: string }> = {
  VALIDATION_ERROR: {
    title: "Please check your details",
    hint: "Something in the form needs adjusting before we can plan your trip.",
  },
  RATE_LIMITED: {
    title: "Too many requests",
    hint: "You've searched a lot in a short time — give it a moment, then try again.",
  },
  NO_PLAN_AVAILABLE: {
    title: "No trip fits those constraints",
    hint: "Try a higher budget, more days, or fewer interests, then plan again.",
  },
  INTERNAL_ERROR: {
    title: "Something went wrong on our side",
    hint: "This one's on us. Please try again in a moment.",
  },
  NETWORK_ERROR: {
    title: "Can't reach the server",
    hint: "Check your connection and try again.",
  },
};

interface ErrorStateProps {
  error: ApiError;
  onRetry?: () => void;
  onEdit?: () => void;
}

export default function ErrorState({
  error,
  onRetry,
  onEdit,
}: ErrorStateProps) {
  const { title, hint } = MESSAGES[error.code] ?? {
    title: "Something went wrong",
    hint: "Please try again.",
  };

  return (
    <div className="py-6 text-center" role="alert">
      <span className="text-4xl" aria-hidden>
        ⚠️
      </span>
      <h2 className="mt-3 text-xl font-bold text-slate-800">{title}</h2>
      <p className="mx-auto mt-2 max-w-md text-slate-600">{hint}</p>
      {error.message && (
        <p className="mx-auto mt-3 max-w-md rounded-lg bg-slate-50 px-3 py-2 text-sm text-slate-500">
          {error.message}
          {error.field ? ` (field: ${error.field})` : ""}
        </p>
      )}
      <div className="mt-6 flex justify-center gap-3">
        {onRetry && (
          <button
            type="button"
            onClick={onRetry}
            className="rounded-xl bg-teal-600 px-5 py-2.5 font-semibold text-white shadow-sm hover:bg-teal-700"
          >
            Try again
          </button>
        )}
        {onEdit && (
          <button
            type="button"
            onClick={onEdit}
            className="rounded-xl border border-slate-200 bg-white px-5 py-2.5 font-medium text-slate-700 hover:border-slate-300"
          >
            Edit details
          </button>
        )}
      </div>
    </div>
  );
}
