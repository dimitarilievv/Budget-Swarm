/**
 * TripContext — app-wide state for the currently generated trip.
 *
 * Holds the latest {@link TripResponse} (or null before anything is generated) so the shell
 * can switch between the planner and the Results page, and any page can read or clear it
 * without prop-drilling. Consume it through the {@link useTripResult} hook.
 */
import { createContext, useMemo, useState, type ReactNode } from "react";

import type { TripRequestPayload, TripResponse } from "@/types/trip";

/** Identifies which saved trip the current view relates to (set when opening one). */
export interface SavedTripRef {
  tripId: number;
  savedAt: string;
  status: "planned" | "reserved";
  transportReserved: boolean;
  stayReserved: boolean;
}

export interface TripContextValue {
  result: TripResponse | null;
  /** The request that produced {@link result} — carries budget, preferences and origin, which
   * the response omits but the Results page needs for the hero and the budget bar. */
  request: TripRequestPayload | null;
  /** Non-null when the current view is (or was opened from) a saved trip — drives the
   * "saved on …, prices may have changed" banner and the Refresh/Update actions. */
  saved: SavedTripRef | null;
  setResult: (
    result: TripResponse | null,
    request?: TripRequestPayload | null,
    saved?: SavedTripRef | null,
  ) => void;
  clear: () => void;
}

// eslint-disable-next-line react-refresh/only-export-components
export const TripContext = createContext<TripContextValue | null>(null);

export function TripProvider({ children }: { children: ReactNode }) {
  const [result, setResultState] = useState<TripResponse | null>(null);
  const [request, setRequest] = useState<TripRequestPayload | null>(null);
  const [saved, setSaved] = useState<SavedTripRef | null>(null);

  const value = useMemo<TripContextValue>(
    () => ({
      result,
      request,
      saved,
      // A new generation clears the saved association (saved defaults to null); opening or
      // refreshing a saved trip passes the ref through to keep the context.
      setResult: (next, nextRequest = null, nextSaved = null) => {
        setResultState(next);
        setRequest(nextRequest);
        setSaved(nextSaved);
      },
      clear: () => {
        setResultState(null);
        setRequest(null);
        setSaved(null);
      },
    }),
    [result, request, saved],
  );

  return <TripContext.Provider value={value}>{children}</TripContext.Provider>;
}
