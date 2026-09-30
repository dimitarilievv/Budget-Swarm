/**
 * MapView — a small Leaflet map that plots a set of real, coordinate-bearing markers over
 * OpenStreetMap tiles.
 *
 * It only ever renders markers it is given actual latitude/longitude for (destination centre,
 * accommodation) — it never invents positions — so a caller with sparse data gets an honest,
 * sparse map. The map is initialised lazily in an effect and every Leaflet call is guarded, so
 * it degrades to an empty (but valid) container under jsdom / SSR instead of throwing.
 *
 * A caller can ask the map to spotlight one marker (e.g. the accommodation's "Show on map"):
 * pass a `focus` with the target coordinates and a changing `nonce`. The map keeps every
 * marker in view (re-fits the full set) and just opens the requested marker's popup to pin
 * it — the other markers are not hidden. The `nonce` makes repeated clicks re-trigger it.
 */
import { useEffect, useRef } from "react";
import L from "leaflet";
import "leaflet/dist/leaflet.css";
import { MARKER_COLOR, type MapMarker } from "./mapMarkers";

function pin(color: string): L.DivIcon {
  return L.divIcon({
    className: "",
    html: `<span style="display:block;width:16px;height:16px;border-radius:9999px;background:${color};border:2px solid #fff;box-shadow:0 1px 4px rgba(15,23,42,.45)"></span>`,
    iconSize: [16, 16],
    iconAnchor: [8, 8],
    popupAnchor: [0, -8],
  });
}

export interface MapFocus {
  lat: number;
  lon: number;
  /** Changing value that re-triggers the focus even when the coordinates repeat. */
  nonce: number;
  zoom?: number;
}

interface MapViewProps {
  markers: MapMarker[];
  className?: string;
  ariaLabel: string;
  /** Zoom used when there is a single marker (no bounds to fit). */
  singleZoom?: number;
  /** When set, recenter on these coordinates and open the matching marker's popup. */
  focus?: MapFocus | null;
}

const _key = (lat: number, lon: number) =>
  `${lat.toFixed(5)},${lon.toFixed(5)}`;

export default function MapView({
  markers,
  className,
  ariaLabel,
  singleZoom = 13,
  focus = null,
}: MapViewProps) {
  const containerRef = useRef<HTMLDivElement>(null);
  const mapRef = useRef<L.Map | undefined>(undefined);
  const markerByKey = useRef<Map<string, L.Marker>>(new Map());
  const pointsRef = useRef<L.LatLngTuple[]>([]);

  useEffect(() => {
    const el = containerRef.current;
    if (!el || markers.length === 0) return;

    let map: L.Map | undefined;
    const byKey = new Map<string, L.Marker>();
    const points: L.LatLngTuple[] = [];
    try {
      map = L.map(el, {
        scrollWheelZoom: false,
        zoomControl: true,
        attributionControl: true,
      });
      L.tileLayer("https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png", {
        maxZoom: 19,
        attribution: "&copy; OpenStreetMap contributors",
      }).addTo(map);

      for (const m of markers) {
        const marker = L.marker([m.lat, m.lon], {
          icon: pin(MARKER_COLOR[m.kind]),
          title: m.label,
        })
          .addTo(map)
          .bindPopup(m.label);
        byKey.set(_key(m.lat, m.lon), marker);
        points.push([m.lat, m.lon]);
      }

      if (points.length === 1) {
        map.setView(points[0], singleZoom);
      } else {
        map.fitBounds(L.latLngBounds(points).pad(0.35));
      }
    } catch {
      // No usable DOM geometry (jsdom / SSR / hidden container): leave an empty container.
    }

    mapRef.current = map;
    markerByKey.current = byKey;
    pointsRef.current = points;

    return () => {
      try {
        map?.remove();
      } catch {
        /* already torn down */
      }
      mapRef.current = undefined;
      markerByKey.current = new Map();
      pointsRef.current = [];
    };
  }, [markers, singleZoom]);

  // Spotlight one marker on request WITHOUT hiding the others: keep every marker in view
  // (re-fit the full set) and just open the requested marker's popup to pin it.
  useEffect(() => {
    if (!focus) return;
    const map = mapRef.current;
    if (!map) return;
    try {
      const points = pointsRef.current;
      if (points.length > 1) {
        map.fitBounds(L.latLngBounds(points).pad(0.35));
      } else if (points.length === 1) {
        map.setView(points[0], singleZoom);
      }
      markerByKey.current.get(_key(focus.lat, focus.lon))?.openPopup();
    } catch {
      /* map not ready (jsdom / hidden) — nothing to spotlight */
    }
  }, [focus, singleZoom]);

  return (
    <div
      ref={containerRef}
      className={className}
      role="img"
      aria-label={ariaLabel}
    />
  );
}
