/**
 * TripContext — app-wide state for the currently generated trip.
 *
 * Holds the latest {@link TripResponse} (or null before anything is generated) so the shell
 * can switch between the planner and the Results page, and any page can read or clear it
 * without prop-drilling. Consume it through the {@link useTripResult} hook.
 */
import { createContext, useMemo, useState, type ReactNode } from "react";

import type { TripRequestPayload, TripResponse } from "@/types/trip";

export interface TripContextValue {
  result: TripResponse | null;
  /** The request that produced {@link result} — carries budget, preferences and origin, which
   * the response omits but the Results page needs for the hero and the budget bar. */
  request: TripRequestPayload | null;
  setResult: (result: TripResponse | null, request?: TripRequestPayload | null) => void;
  clear: () => void;
}

// eslint-disable-next-line react-refresh/only-export-components
export const TripContext = createContext<TripContextValue | null>(null);

export function TripProvider({ children }: { children: ReactNode }) {
  const [result, setResultState] = useState<TripResponse | null>(null);
  const [request, setRequest] = useState<TripRequestPayload | null>(null);

  const value = useMemo<TripContextValue>(
    () => ({
      result,
      request,
      setResult: (next, nextRequest = null) => {
        setResultState(next);
        setRequest(nextRequest);
      },
      clear: () => {
        setResultState(null);
        setRequest(null);
      },
    }),
    [result, request],
  );

  return <TripContext.Provider value={value}>{children}</TripContext.Provider>;
}
