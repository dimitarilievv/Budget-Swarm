/**
 * MapView — a small Leaflet map that plots a set of real, coordinate-bearing markers over
 * OpenStreetMap tiles.
 *
 * It only ever renders markers it is given actual latitude/longitude for (destination centre,
 * accommodation) — it never invents positions — so a caller with sparse data gets an honest,
 * sparse map. The map is initialised lazily in an effect and every Leaflet call is guarded, so
 * it degrades to an empty (but valid) container under jsdom / SSR instead of throwing.
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

interface MapViewProps {
  markers: MapMarker[];
  className?: string;
  ariaLabel: string;
  /** Zoom used when there is a single marker (no bounds to fit). */
  singleZoom?: number;
}

export default function MapView({
  markers,
  className,
  ariaLabel,
  singleZoom = 13,
}: MapViewProps) {
  const containerRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    const el = containerRef.current;
    if (!el || markers.length === 0) return;

    let map: L.Map | undefined;
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

      const points: L.LatLngTuple[] = [];
      for (const m of markers) {
        L.marker([m.lat, m.lon], {
          icon: pin(MARKER_COLOR[m.kind]),
          title: m.label,
        })
          .addTo(map)
          .bindPopup(m.label);
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

    return () => {
      try {
        map?.remove();
      } catch {
        /* already torn down */
      }
    };
  }, [markers, singleZoom]);

  return (
    <div
      ref={containerRef}
      className={className}
      role="img"
      aria-label={ariaLabel}
    />
  );
}
