/**
 * Shared marker vocabulary for {@link MapView} and its legends.
 *
 * Kept in its own module (not in MapView.tsx) so the map component file exports only a
 * component — that keeps React Fast Refresh working and lets legends import the colour scale
 * without pulling in Leaflet.
 */
export type MarkerKind =
  "destination" | "accommodation" | "activity" | "airport";

export interface MapMarker {
  lat: number;
  lon: number;
  label: string;
  kind: MarkerKind;
  /** When set, the pin shows this number (an itinerary stop's position) instead of a plain dot,
   * and the map can spotlight it by number on hover. */
  number?: number;
}

/** Marker colours, shared with map legends so a pin and its legend swatch always match. */
export const MARKER_COLOR: Record<MarkerKind, string> = {
  destination: "#0d9488", // teal-600
  accommodation: "#f59e0b", // amber-500
  activity: "#8b5cf6", // violet-500
  airport: "#0ea5e9", // sky-500
};
