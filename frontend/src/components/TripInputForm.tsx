/**
 * TripInputForm — the trip Input page (BS-084).
 *
 * Collects every documented constraint (budget, duration, travelers, transport, weighted
 * preferences, and — only for car — vehicle_consumption) plus the origin, validates it
 * client-side (mirroring the server, which stays authoritative), and submits a typed
 * TripRequestPayload to POST /api/generate-trip. On success it shows a short plan summary;
 * an ApiError is surfaced with its message. `submit` is injectable for tests.
 */
import { useId, useState, type FormEvent } from "react";

import { ApiError } from "../api/errors";
import { generateTrip } from "../api/trips";
import type {
  TransportMode,
  TripRequestPayload,
  TripResponse,
} from "../api/types";
import OriginInput, { type OriginValue } from "./OriginInput";

const TRANSPORT_MODES: { value: TransportMode; label: string }[] = [
  { value: "car", label: "Car" },
  { value: "airplane", label: "Airplane" },
  { value: "bus", label: "Bus" },
  { value: "train", label: "Train" },
];

const PREFERENCE_THEMES = [
  "culture",
  "nature",
  "nightlife",
  "food",
  "tourism",
] as const;

interface TripInputFormProps {
  onResult?: (response: TripResponse) => void;
  submit?: (payload: TripRequestPayload) => Promise<TripResponse>;
}

type Errors = Partial<
  Record<
    | "budget"
    | "duration_days"
    | "travelers"
    | "vehicle_consumption"
    | "preferences"
    | "origin",
    string
  >
>;

export default function TripInputForm({
  onResult,
  submit = generateTrip,
}: TripInputFormProps) {
  const ids = {
    budget: useId(),
    duration: useId(),
    travelers: useId(),
    transport: useId(),
    fuel: useId(),
  };

  const [budget, setBudget] = useState("1000");
  const [durationDays, setDurationDays] = useState("5");
  const [travelers, setTravelers] = useState("2");
  const [transportMode, setTransportMode] = useState<TransportMode>("car");
  const [vehicleConsumption, setVehicleConsumption] = useState("6.5");
  const [preferences, setPreferences] = useState<Record<string, number>>({
    culture: 0.8,
    food: 0.6,
    nature: 0,
    nightlife: 0,
    tourism: 0,
  });
  const [origin, setOrigin] = useState<OriginValue>({
    originLabel: "",
    originLat: null,
    originLon: null,
  });

  const [errors, setErrors] = useState<Errors>({});
  const [submitting, setSubmitting] = useState(false);
  const [submitError, setSubmitError] = useState<string | null>(null);
  const [result, setResult] = useState<TripResponse | null>(null);

  const isCar = transportMode === "car";

  function validate(): Errors {
    const e: Errors = {};
    if (!(parseFloat(budget) > 0)) e.budget = "Budget must be greater than 0.";
    const d = Number(durationDays);
    if (!Number.isInteger(d) || d < 1) e.duration_days = "At least 1 day.";
    const t = Number(travelers);
    if (!Number.isInteger(t) || t < 1) e.travelers = "At least 1 traveler.";
    if (!origin.originLabel.trim())
      e.origin = "Enter where you're starting from.";
    if (Object.values(preferences).every((w) => w <= 0))
      e.preferences = "Pick at least one preference.";
    if (isCar && !(parseFloat(vehicleConsumption) > 0))
      e.vehicle_consumption = "Fuel use (L/100 km) is required for car trips.";
    return e;
  }

  function buildPayload(): TripRequestPayload {
    const prefs: Record<string, number> = {};
    for (const [k, w] of Object.entries(preferences)) if (w > 0) prefs[k] = w;
    return {
      budget: parseFloat(budget),
      duration_days: Number(durationDays),
      travelers: Number(travelers),
      transport_mode: transportMode,
      preferences: prefs,
      origin_label: origin.originLabel.trim(),
      origin_lat: origin.originLat,
      origin_lon: origin.originLon,
      vehicle_consumption: isCar ? parseFloat(vehicleConsumption) : null,
    };
  }

  async function handleSubmit(event: FormEvent) {
    event.preventDefault();
    const found = validate();
    setErrors(found);
    if (Object.keys(found).length > 0) return;

    setSubmitting(true);
    setSubmitError(null);
    setResult(null);
    try {
      const response = await submit(buildPayload());
      setResult(response);
      onResult?.(response);
    } catch (err) {
      setSubmitError(
        err instanceof ApiError
          ? err.message
          : "Something went wrong. Please try again.",
      );
    } finally {
      setSubmitting(false);
    }
  }

  const fieldClass = (invalid: boolean) =>
    `mt-1 w-full rounded-lg border px-3 py-2 text-slate-900 focus:outline-none focus:ring-1 ${
      invalid
        ? "border-amber-400 focus:border-amber-500 focus:ring-amber-500"
        : "border-slate-300 focus:border-indigo-500 focus:ring-indigo-500"
    }`;

  return (
    <form onSubmit={handleSubmit} noValidate className="space-y-5 text-left">
      <OriginInput value={origin} onChange={setOrigin} />
      {errors.origin && (
        <p className="text-xs text-amber-600">{errors.origin}</p>
      )}

      <div className="grid grid-cols-2 gap-4">
        <div>
          <label
            htmlFor={ids.budget}
            className="text-sm font-medium text-slate-700"
          >
            Budget (EUR)
          </label>
          <input
            id={ids.budget}
            type="number"
            min="1"
            step="1"
            value={budget}
            aria-invalid={Boolean(errors.budget)}
            onChange={(e) => setBudget(e.target.value)}
            className={fieldClass(Boolean(errors.budget))}
          />
          {errors.budget && (
            <p className="mt-1 text-xs text-amber-600">{errors.budget}</p>
          )}
        </div>

        <div>
          <label
            htmlFor={ids.duration}
            className="text-sm font-medium text-slate-700"
          >
            Trip length (days)
          </label>
          <input
            id={ids.duration}
            type="number"
            min="1"
            step="1"
            value={durationDays}
            aria-invalid={Boolean(errors.duration_days)}
            onChange={(e) => setDurationDays(e.target.value)}
            className={fieldClass(Boolean(errors.duration_days))}
          />
          {errors.duration_days && (
            <p className="mt-1 text-xs text-amber-600">
              {errors.duration_days}
            </p>
          )}
        </div>

        <div>
          <label
            htmlFor={ids.travelers}
            className="text-sm font-medium text-slate-700"
          >
            Travelers
          </label>
          <input
            id={ids.travelers}
            type="number"
            min="1"
            step="1"
            value={travelers}
            aria-invalid={Boolean(errors.travelers)}
            onChange={(e) => setTravelers(e.target.value)}
            className={fieldClass(Boolean(errors.travelers))}
          />
          {errors.travelers && (
            <p className="mt-1 text-xs text-amber-600">{errors.travelers}</p>
          )}
        </div>

        <div>
          <label
            htmlFor={ids.transport}
            className="text-sm font-medium text-slate-700"
          >
            Transport
          </label>
          <select
            id={ids.transport}
            value={transportMode}
            onChange={(e) => setTransportMode(e.target.value as TransportMode)}
            className={fieldClass(false)}
          >
            {TRANSPORT_MODES.map((m) => (
              <option key={m.value} value={m.value}>
                {m.label}
              </option>
            ))}
          </select>
        </div>
      </div>

      {isCar && (
        <div>
          <label
            htmlFor={ids.fuel}
            className="text-sm font-medium text-slate-700"
          >
            Fuel use (L/100 km)
          </label>
          <input
            id={ids.fuel}
            type="number"
            min="0.1"
            step="0.1"
            value={vehicleConsumption}
            aria-invalid={Boolean(errors.vehicle_consumption)}
            onChange={(e) => setVehicleConsumption(e.target.value)}
            className={fieldClass(Boolean(errors.vehicle_consumption))}
          />
          {errors.vehicle_consumption && (
            <p className="mt-1 text-xs text-amber-600">
              {errors.vehicle_consumption}
            </p>
          )}
        </div>
      )}

      <fieldset>
        <legend className="text-sm font-medium text-slate-700">
          What matters most?{" "}
          <span className="font-normal text-slate-400">(weight each)</span>
        </legend>
        <div className="mt-2 space-y-2">
          {PREFERENCE_THEMES.map((theme) => (
            <div key={theme} className="flex items-center gap-3">
              <label
                htmlFor={`pref-${theme}`}
                className="w-24 text-sm capitalize text-slate-600"
              >
                {theme}
              </label>
              <input
                id={`pref-${theme}`}
                type="range"
                min="0"
                max="1"
                step="0.1"
                value={preferences[theme]}
                onChange={(e) =>
                  setPreferences((p) => ({
                    ...p,
                    [theme]: Number(e.target.value),
                  }))
                }
                className="flex-1 accent-indigo-600"
              />
              <span className="w-8 text-right text-xs tabular-nums text-slate-500">
                {preferences[theme].toFixed(1)}
              </span>
            </div>
          ))}
        </div>
        {errors.preferences && (
          <p className="mt-1 text-xs text-amber-600">{errors.preferences}</p>
        )}
      </fieldset>

      <button
        type="submit"
        disabled={submitting}
        className="w-full rounded-lg bg-indigo-600 px-4 py-2.5 font-medium text-white hover:bg-indigo-700 disabled:opacity-60"
      >
        {submitting ? "Planning your trip…" : "Plan my trip"}
      </button>

      <div aria-live="polite">
        {submitError && (
          <p className="rounded-lg bg-amber-50 px-3 py-2 text-sm text-amber-700">
            {submitError}
          </p>
        )}
        {result && (
          <div className="rounded-lg bg-emerald-50 px-3 py-2 text-sm text-emerald-800">
            <p className="font-medium">
              {result.chosen_plan.destination.city},{" "}
              {result.chosen_plan.destination.country} — €
              {Math.round(result.chosen_plan.total_cost)}
            </p>
            {result.explanation && (
              <p className="mt-1 text-emerald-700">{result.explanation}</p>
            )}
          </div>
        )}
      </div>
    </form>
  );
}
