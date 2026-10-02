/**
 * TripInputForm — the trip Input page card (BS-084 / redesign).
 *
 * Collects every documented constraint (budget, duration, travelers, transport, weighted
 * interests, and — only for car — vehicle_consumption) plus the origin, validates it
 * client-side (mirroring the server, which stays authoritative), and submits a typed
 * TripRequestPayload to POST /api/generate-trip. `submit` is injectable for tests.
 */
import {
  useId,
  useRef,
  useState,
  type ComponentType,
  type FormEvent,
  type SVGProps,
} from "react";

import { ApiError } from "@/api/errors";
import { generateTrip } from "@/api/endpoints";
import type {
  TransportMode,
  TripRequestPayload,
  TripResponse,
} from "@/types/trip";
import ErrorState from "@/ui/components/common/ErrorState";
import LoadingState from "@/ui/components/common/LoadingState";
import NoTripFound from "@/ui/components/common/NoTripFound";
import OriginInput, { type OriginValue } from "./OriginInput";
import {
  IconArrowRight,
  IconBus,
  IconCamera,
  IconCar,
  IconLandmark,
  IconMoon,
  IconMountain,
  IconPlane,
  IconTrain,
  IconUtensils,
} from "@/ui/components/common/Icons";
import { INPUT_CLASS } from "@/ui/theme/theme";

type IconType = ComponentType<SVGProps<SVGSVGElement>>;

const TRANSPORT_MODES: {
  value: TransportMode;
  label: string;
  Icon: IconType;
}[] = [
  { value: "airplane", label: "Airplane", Icon: IconPlane },
  { value: "car", label: "Car", Icon: IconCar },
  { value: "bus", label: "Bus", Icon: IconBus },
  { value: "train", label: "Train", Icon: IconTrain },
];

// Interests map to the engine's preference themes (culture/nature/nightlife/food/tourism).
const INTERESTS: { key: string; label: string; Icon: IconType }[] = [
  { key: "culture", label: "Culture", Icon: IconLandmark },
  { key: "nature", label: "Nature", Icon: IconMountain },
  { key: "food", label: "Food", Icon: IconUtensils },
  { key: "nightlife", label: "Nightlife", Icon: IconMoon },
  { key: "tourism", label: "Sightseeing", Icon: IconCamera },
];
const DEFAULT_INTERESTS = ["culture", "food", "tourism"];

interface TripInputFormProps {
  /** Receives the plan and the request that produced it (the request carries budget,
   * preferences and origin, which the Results page needs but the response omits). */
  onResult?: (response: TripResponse, request: TripRequestPayload) => void;
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
    fuel: useId(),
  };

  const [budget, setBudget] = useState("1000");
  const [durationDays, setDurationDays] = useState("5");
  const [travelers, setTravelers] = useState(2);
  const [transportMode, setTransportMode] = useState<TransportMode>("car");
  const [vehicleConsumption, setVehicleConsumption] = useState("6.5");
  const [interests, setInterests] = useState<Set<string>>(
    new Set(DEFAULT_INTERESTS),
  );
  const [origin, setOrigin] = useState<OriginValue>({
    originLabel: "",
    originLat: null,
    originLon: null,
  });

  const [errors, setErrors] = useState<Errors>({});
  const [submitting, setSubmitting] = useState(false);
  const [submitError, setSubmitError] = useState<ApiError | null>(null);
  const lastPayload = useRef<TripRequestPayload | null>(null);

  const isCar = transportMode === "car";

  function toggleInterest(key: string) {
    setInterests((prev) => {
      const next = new Set(prev);
      if (next.has(key)) next.delete(key);
      else next.add(key);
      return next;
    });
  }

  function validate(): Errors {
    const e: Errors = {};
    if (!(parseFloat(budget) > 0)) e.budget = "Budget must be greater than 0.";
    const d = Number(durationDays);
    if (!Number.isInteger(d) || d < 1) e.duration_days = "At least 1 day.";
    if (!Number.isInteger(travelers) || travelers < 1)
      e.travelers = "At least 1 traveler.";
    if (!origin.originLabel.trim())
      e.origin = "Enter where you're starting from.";
    if (interests.size === 0) e.preferences = "Pick at least one interest.";
    if (isCar && !(parseFloat(vehicleConsumption) > 0))
      e.vehicle_consumption =
        "Vehicle consumption (L/100 km) is required for car trips.";
    return e;
  }

  function buildPayload(): TripRequestPayload {
    const preferences: Record<string, number> = {};
    for (const key of interests) preferences[key] = 1.0;
    return {
      budget: parseFloat(budget),
      duration_days: Number(durationDays),
      travelers,
      transport_mode: transportMode,
      preferences,
      origin_label: origin.originLabel.trim(),
      origin_lat: origin.originLat,
      origin_lon: origin.originLon,
      vehicle_consumption: isCar ? parseFloat(vehicleConsumption) : null,
    };
  }

  async function runSubmit(payload: TripRequestPayload) {
    lastPayload.current = payload;
    setSubmitting(true);
    setSubmitError(null);
    try {
      const response = await submit(payload);
      onResult?.(response, payload);
    } catch (err) {
      setSubmitError(
        err instanceof ApiError
          ? err
          : new ApiError(
              "INTERNAL_ERROR",
              "Something went wrong. Please try again.",
            ),
      );
    } finally {
      setSubmitting(false);
    }
  }

  async function handleSubmit(event: FormEvent) {
    event.preventDefault();
    const found = validate();
    setErrors(found);
    if (Object.keys(found).length > 0) return;
    await runSubmit(buildPayload());
  }

  function retry() {
    if (lastPayload.current) void runSubmit(lastPayload.current);
  }

  const numberField = INPUT_CLASS;

  // While a request is in flight, show the agent-progress screen; on failure, the error
  // state with a retry affordance. Form state persists behind both (same component).
  if (submitting) return <LoadingState />;
  if (submitError) {
    // A no-plan result is an outcome, not a failure: guide the user to adjust their inputs
    // rather than offering a retry of the identical (and equally infeasible) request.
    if (submitError.code === "NO_PLAN_AVAILABLE")
      return (
        <NoTripFound
          budget={parseFloat(budget)}
          message={submitError.message}
          onAdjust={() => setSubmitError(null)}
        />
      );
    return (
      <ErrorState
        error={submitError}
        onRetry={retry}
        onEdit={() => setSubmitError(null)}
      />
    );
  }

  return (
    <form onSubmit={handleSubmit} noValidate className="space-y-6 text-left">
      <OriginInput value={origin} onChange={setOrigin} />
      {errors.origin && (
        <p className="text-xs text-amber-600">{errors.origin}</p>
      )}

      {/* Budget · Duration · Travelers */}
      <div className="grid gap-4 sm:grid-cols-3">
        <div>
          <label
            htmlFor={ids.budget}
            className="text-sm font-semibold text-slate-800"
          >
            Budget (total)
          </label>
          <div className="relative">
            <input
              id={ids.budget}
              type="number"
              inputMode="decimal"
              min="1"
              value={budget}
              aria-invalid={Boolean(errors.budget)}
              onChange={(e) => setBudget(e.target.value)}
              className={`${numberField} pr-9`}
            />
            <span className="pointer-events-none absolute right-4 top-1/2 -translate-y-1/2 text-slate-400">
              €
            </span>
          </div>
          {errors.budget && (
            <p className="mt-1 text-xs text-amber-600">{errors.budget}</p>
          )}
        </div>

        <div>
          <label
            htmlFor={ids.duration}
            className="text-sm font-semibold text-slate-800"
          >
            Trip duration
          </label>
          <div className="relative">
            <input
              id={ids.duration}
              type="number"
              min="1"
              value={durationDays}
              aria-invalid={Boolean(errors.duration_days)}
              onChange={(e) => setDurationDays(e.target.value)}
              className={`${numberField} pr-14`}
            />
            <span className="pointer-events-none absolute right-4 top-1/2 -translate-y-1/2 text-sm text-slate-400">
              days
            </span>
          </div>
          {errors.duration_days && (
            <p className="mt-1 text-xs text-amber-600">
              {errors.duration_days}
            </p>
          )}
        </div>

        <div>
          <label
            htmlFor={ids.travelers}
            className="text-sm font-semibold text-slate-800"
          >
            Travelers
          </label>
          <div className="mt-1.5 flex items-center rounded-lg border border-slate-200 bg-white shadow-sm">
            <button
              type="button"
              aria-label="Fewer travelers"
              onClick={() => setTravelers((n) => Math.max(1, n - 1))}
              className="px-3.5 py-2.5 text-lg text-slate-500 hover:text-teal-600"
            >
              −
            </button>
            <input
              id={ids.travelers}
              type="number"
              min="1"
              value={travelers}
              aria-invalid={Boolean(errors.travelers)}
              onChange={(e) => setTravelers(Number(e.target.value))}
              className="w-full border-x border-slate-100 py-2.5 text-center text-[15px] font-medium text-slate-900 focus:outline-none"
            />
            <button
              type="button"
              aria-label="More travelers"
              onClick={() => setTravelers((n) => n + 1)}
              className="px-3.5 py-2.5 text-lg text-slate-500 hover:text-teal-600"
            >
              +
            </button>
          </div>
          {errors.travelers && (
            <p className="mt-1 text-xs text-amber-600">{errors.travelers}</p>
          )}
        </div>
      </div>

      {/* Transport mode — segmented cards */}
      <div>
        <span className="text-sm font-semibold text-slate-800">
          Transport mode{" "}
          <span className="font-normal text-slate-400">
            (from your location)
          </span>
        </span>
        <div className="mt-2 grid grid-cols-2 gap-2 sm:grid-cols-4">
          {TRANSPORT_MODES.map((m) => {
            const active = transportMode === m.value;
            return (
              <button
                key={m.value}
                type="button"
                aria-pressed={active}
                onClick={() => setTransportMode(m.value)}
                className={`flex flex-col items-center gap-1.5 rounded-xl border px-3 py-3 text-sm font-medium transition ${
                  active
                    ? "border-teal-500 bg-teal-50 text-teal-700 ring-1 ring-teal-500/40"
                    : "border-slate-200 bg-white text-slate-600 hover:border-slate-300"
                }`}
              >
                <m.Icon className="h-5 w-5" />
                {m.label}
              </button>
            );
          })}
        </div>
      </div>

      {/* Conditional: vehicle consumption for car */}
      {isCar && (
        <div>
          <label
            htmlFor={ids.fuel}
            className="text-sm font-semibold text-slate-800"
          >
            Vehicle consumption{" "}
            <span className="font-normal text-slate-400">(L/100 km)</span>
          </label>
          <input
            id={ids.fuel}
            type="number"
            min="0.1"
            step="0.1"
            value={vehicleConsumption}
            aria-invalid={Boolean(errors.vehicle_consumption)}
            onChange={(e) => setVehicleConsumption(e.target.value)}
            className={numberField}
          />
          {errors.vehicle_consumption && (
            <p className="mt-1 text-xs text-amber-600">
              {errors.vehicle_consumption}
            </p>
          )}
        </div>
      )}

      {/* Interests — chips */}
      <div>
        <span className="text-sm font-semibold text-slate-800">
          Interests{" "}
          <span className="font-normal text-slate-400">
            (pick your favorites)
          </span>
        </span>
        <div className="mt-2 flex flex-wrap gap-2">
          {INTERESTS.map((it) => {
            const active = interests.has(it.key);
            return (
              <button
                key={it.key}
                type="button"
                aria-pressed={active}
                onClick={() => toggleInterest(it.key)}
                className={`flex items-center gap-2 rounded-full border px-4 py-2 text-sm font-medium transition ${
                  active
                    ? "border-teal-500 bg-teal-50 text-teal-700"
                    : "border-slate-200 bg-white text-slate-600 hover:border-slate-300"
                }`}
              >
                <it.Icon className="h-4 w-4" />
                {it.label}
              </button>
            );
          })}
        </div>
        {errors.preferences && (
          <p className="mt-1 text-xs text-amber-600">{errors.preferences}</p>
        )}
      </div>

      {/* CTA */}
      <button
        type="submit"
        className="flex w-full items-center justify-center gap-2 rounded-xl bg-gradient-to-r from-teal-600 to-teal-500 px-4 py-3.5 text-base font-semibold text-white shadow-sm transition hover:from-teal-700 hover:to-teal-600 focus:outline-none focus-visible:ring-2 focus-visible:ring-teal-500/40"
      >
        Generate Optimized Plan
        <IconArrowRight className="h-5 w-5" />
      </button>
    </form>
  );
}
