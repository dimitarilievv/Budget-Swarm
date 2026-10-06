/**
 * API error envelope handling (docs/api.md).
 *
 * Every backend failure comes back as `{ error: { code, message, field? } }` with one of
 * the documented codes. `toApiError` turns any thrown request failure — an enveloped
 * backend error, a raw HTTP error, or a network failure — into a single typed
 * {@link ApiError} the UI can catch and display consistently.
 */
import axios from "axios";

/** The documented backend error codes (api.md), plus a client-only network sentinel. */
export type ApiErrorCode =
  | "VALIDATION_ERROR"
  | "UNAUTHORIZED"
  | "NOT_FOUND"
  | "CONFLICT"
  | "RATE_LIMITED"
  | "NO_PLAN_AVAILABLE"
  | "INTERNAL_ERROR"
  | "NETWORK_ERROR";

export interface ApiErrorEnvelope {
  error: { code: string; message: string; field?: string };
}

export class ApiError extends Error {
  /** The api.md error code (or `NETWORK_ERROR` when the server was unreachable). */
  readonly code: ApiErrorCode | string;
  /** The offending input field, present only for validation errors. */
  readonly field?: string;
  /** The HTTP status, when the server responded. */
  readonly status?: number;

  constructor(
    code: ApiErrorCode | string,
    message: string,
    options: { field?: string; status?: number } = {},
  ) {
    super(message);
    this.name = "ApiError";
    this.code = code;
    this.field = options.field;
    this.status = options.status;
  }
}

function isEnvelope(data: unknown): data is ApiErrorEnvelope {
  if (typeof data !== "object" || data === null) return false;
  const err = (data as { error?: unknown }).error;
  return (
    typeof err === "object" &&
    err !== null &&
    typeof (err as { code?: unknown }).code === "string" &&
    typeof (err as { message?: unknown }).message === "string"
  );
}

/** Normalize any request failure into a typed {@link ApiError}. */
export function toApiError(err: unknown): ApiError {
  if (axios.isAxiosError(err)) {
    const status = err.response?.status;
    const data = err.response?.data;
    if (isEnvelope(data)) {
      return new ApiError(data.error.code, data.error.message, {
        field: data.error.field,
        status,
      });
    }
    if (err.response === undefined) {
      // No response at all — the server was unreachable / the request timed out.
      return new ApiError(
        "NETWORK_ERROR",
        "Couldn't reach the server. Check your connection and try again.",
        { status },
      );
    }
    return new ApiError(
      "INTERNAL_ERROR",
      err.message || "Unexpected server error.",
      {
        status,
      },
    );
  }
  return new ApiError(
    "INTERNAL_ERROR",
    err instanceof Error ? err.message : "Unexpected error.",
  );
}
