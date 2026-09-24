/**
 * OriginInput — the trip's starting point.
 *
 * A controlled field for where the trip starts. "Use my location" resolves the browser's
 * geolocation to coordinates + a readable label (what the routing engine needs); the user
 * can always type a city instead, which the backend geocodes server-side. Editing the
 * field by hand clears any detected coordinates, so a typed city is never paired with a
 * stale position. When geolocation is unavailable or denied, a friendly message steers the
 * user to the manual input — no dead end.
 *
 * `getOrigin` is injectable so the component is unit-tested without a real browser.
 */
import { useId, useState } from "react";

import {
  type BrowserOrigin,
  GeolocationError,
  getBrowserOrigin,
} from "@/api/geolocation";
import { IconLocation } from "@/ui/components/common/Icons";
import { INPUT_CLASS } from "@/ui/theme/theme";

export interface OriginValue {
  originLabel: string;
  originLat: number | null;
  originLon: number | null;
}

interface OriginInputProps {
  value: OriginValue;
  onChange: (value: OriginValue) => void;
  getOrigin?: () => Promise<BrowserOrigin>;
}

type Status = "idle" | "locating" | "detected" | "error";

const ERROR_MESSAGE: Record<string, string> = {
  permission_denied:
    "Location access was blocked — type your starting city instead.",
  unsupported:
    "This browser can't share your location — type your starting city.",
  timeout: "Getting your location took too long — type your starting city.",
  unavailable: "Couldn't get your location — type your starting city.",
};

export default function OriginInput({
  value,
  onChange,
  getOrigin = getBrowserOrigin,
}: OriginInputProps) {
  const inputId = useId();
  const [status, setStatus] = useState<Status>("idle");
  const [error, setError] = useState<string | null>(null);

  const hasCoords = value.originLat !== null && value.originLon !== null;

  function handleManualEdit(label: string) {
    setStatus("idle");
    setError(null);
    // A hand-typed city drops any detected coordinates (they no longer match).
    onChange({ originLabel: label, originLat: null, originLon: null });
  }

  async function detect() {
    setStatus("locating");
    setError(null);
    try {
      const origin = await getOrigin();
      onChange({
        originLabel: origin.originLabel,
        originLat: origin.originLat,
        originLon: origin.originLon,
      });
      setStatus("detected");
    } catch (err) {
      const kind = err instanceof GeolocationError ? err.kind : "unavailable";
      setError(ERROR_MESSAGE[kind] ?? ERROR_MESSAGE.unavailable);
      setStatus("error");
    }
  }

  return (
    <div className="text-left">
      <div className="flex items-center justify-between">
        <label
          htmlFor={inputId}
          className="text-sm font-semibold text-slate-800"
        >
          Starting from
        </label>
        <button
          type="button"
          onClick={detect}
          disabled={status === "locating"}
          className="flex items-center gap-1 text-xs font-medium text-teal-600 hover:text-teal-800 disabled:opacity-50"
        >
          {status === "locating" ? (
            "Locating…"
          ) : (
            <>
              <IconLocation className="h-3.5 w-3.5" />
              Use my location
            </>
          )}
        </button>
      </div>

      <input
        id={inputId}
        type="text"
        value={value.originLabel}
        onChange={(e) => handleManualEdit(e.target.value)}
        placeholder="e.g. Skopje"
        className={INPUT_CLASS}
      />

      <p className="mt-1 min-h-[1.25rem] text-xs" aria-live="polite">
        {status === "error" && error ? (
          <span className="text-amber-600">{error}</span>
        ) : status === "detected" && hasCoords ? (
          <span className="inline-flex items-center gap-1 text-emerald-600">
            <IconLocation className="h-3.5 w-3.5" />
            Using your location ({value.originLat!.toFixed(3)},{" "}
            {value.originLon!.toFixed(3)})
          </span>
        ) : value.originLabel.trim() ? (
          <span className="text-slate-400">We'll look up this city.</span>
        ) : null}
      </p>
    </div>
  );
}
