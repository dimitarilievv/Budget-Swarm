/**
 * TripContext — app-wide state for the currently generated trip.
 *
 * Holds the latest {@link TripResponse} (or null before anything is generated) so the shell
 * can switch between the planner and the Results page, and any page can read or clear it
 * without prop-drilling. Consume it through the {@link useTripResult} hook.
 */
import { createContext, useMemo, useState, type ReactNode } from "react";

import type { TripResponse } from "@/types/trip";

export interface TripContextValue {
  result: TripResponse | null;
  setResult: (result: TripResponse | null) => void;
  clear: () => void;
}

// eslint-disable-next-line react-refresh/only-export-components
export const TripContext = createContext<TripContextValue | null>(null);

export function TripProvider({ children }: { children: ReactNode }) {
  const [result, setResult] = useState<TripResponse | null>(null);

  const value = useMemo<TripContextValue>(
    () => ({ result, setResult, clear: () => setResult(null) }),
    [result],
  );

  return <TripContext.Provider value={value}>{children}</TripContext.Provider>;
}
