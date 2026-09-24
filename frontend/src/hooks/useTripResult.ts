/**
 * useTripResult — read and update the app-wide generated trip.
 *
 * Thin accessor over {@link TripContext}; throws if used outside a <TripProvider> so the
 * mistake surfaces immediately rather than as a silent null.
 */
import { useContext } from "react";

import { TripContext, type TripContextValue } from "@/contexts/TripContext";

export function useTripResult(): TripContextValue {
  const ctx = useContext(TripContext);
  if (!ctx) {
    throw new Error("useTripResult must be used within a <TripProvider>");
  }
  return ctx;
}
