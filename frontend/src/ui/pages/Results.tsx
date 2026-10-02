/**
 * ResultsPage — the redesigned trip results view (design 1a).
 *
 * Renders the chosen plan of a {@link TripResponse} as one coherent trip: a hero (match score,
 * city, dates, a one-line rationale and a Wikipedia city photo), a budget-aware summary strip,
 * the weather, flights + stay side by side, and a "step by step" journey (fly out → day 1..N →
 * fly home) whose day view lists the stops beside a numbered Leaflet map, with arrival/airport
 * notes drawn from the flight times. Degraded/estimated runs are flagged with a compact chip.
 *
 * Purely presentational: it takes the response plus the originating request (for budget,
 * preferences and origin — which the response omits) and renders straight from fixtures in tests.
 * The city photo and any map pins come only from real coordinates/sources; nothing is invented.
 */
import { useEffect, useMemo, useState } from "react";

import { fetchDestinationSummary } from "@/api/wikipedia";
import type {
  ChosenPlan,
  ItineraryActivity,
  PlanAccommodation,
  PlanTransport,
  PlanWeatherDay,
  TransportMode,
  TripRequestPayload,
  TripResponse,
} from "@/types/trip";
import MapView, { type MapFocus } from "@/ui/components/common/MapView";
import type { MapMarker } from "@/ui/components/common/mapMarkers";
import {
  IconArrowRight,
  IconLocation,
  IconBus,
  IconCar,
  IconPlane,
  IconTrain,
} from "@/ui/components/common/Icons";

/* -------------------------------------------------------------------------- */
/* Design tokens (spec)                                                         */
/* -------------------------------------------------------------------------- */

const C = {
  teal: "#0f9488",
  tealDark: "#0b7a70",
  tealTint: "#e6f5f2",
  indigo: "#6366f1",
  amber: "#f59e0b",
  ink: "#12201e",
  muted: "#5d6b69",
  line: "#e4ebe9",
  bg: "#f4f8f7",
  soft: "#f1f5f4",
  warnBg: "#fff6e0",
  warnInk: "#8a5a00",
  airbnb: "#ff385c",
};

const ENV: Record<string, { label: string; bg: string; fg: string }> = {
  indoor: { label: "Indoor", bg: "#eef0fb", fg: "#4b55c7" },
  outdoor: { label: "Outdoor", bg: "#e7f6ee", fg: "#1f8a55" },
  mixed: { label: "Mixed", bg: "#f1f3f2", fg: C.muted },
};

const CARD: React.CSSProperties = {
  background: "#fff",
  border: `1px solid ${C.line}`,
  borderRadius: 18,
};

/* -------------------------------------------------------------------------- */
/* Formatting helpers                                                           */
/* -------------------------------------------------------------------------- */

function euro(value: number): string {
  return new Intl.NumberFormat("en-IE", {
    style: "currency",
    currency: "EUR",
    maximumFractionDigits: 0,
  }).format(value);
}

/** overall_score is a 0..1 unit score; present it as a familiar /100 match score. */
function scoreOutOf100(score: number): number {
  return Math.round(Math.max(0, Math.min(1, score)) * 100);
}

const TRANSPORT_ICON: Record<TransportMode, typeof IconCar> = {
  airplane: IconPlane,
  car: IconCar,
  bus: IconBus,
  train: IconTrain,
};

const TRANSPORT_WORD: Record<TransportMode, string> = {
  airplane: "Flights",
  car: "Car",
  bus: "Bus",
  train: "Train",
};

function toMin(hhmm?: string | null): number | null {
  if (!hhmm) return null;
  const m = /^(\d{1,2}):(\d{2})/.exec(hhmm);
  if (!m) return null;
  return Number(m[1]) * 60 + Number(m[2]);
}

function fmtDurMin(min: number): string {
  if (min < 60) return `${min}m`;
  const h = Math.floor(min / 60);
  const m = min % 60;
  return m ? `${h}h ${m}m` : `${h}h`;
}

function fmtRange(start?: string | null, end?: string | null): string | null {
  const a = toMin(start);
  const b = toMin(end);
  if (a == null || b == null) return start ?? null;
  return fmtDurMin(Math.max(0, b - a));
}

function weekday(iso?: string | null): string {
  if (!iso) return "";
  const d = new Date(iso);
  return Number.isNaN(d.getTime())
    ? ""
    : d.toLocaleDateString("en-GB", { weekday: "short" });
}

function dayMonth(iso?: string | null): string {
  if (!iso) return "";
  const d = new Date(iso);
  return Number.isNaN(d.getTime())
    ? ""
    : d.toLocaleDateString("en-GB", { day: "numeric", month: "short" });
}

/** "Thu 22 – Mon 26 Oct 2026" for the trip window, or null when no dates are present. */
function formatDateRange(
  start?: string | null,
  end?: string | null,
): string | null {
  if (!start && !end) return null;
  const s = start ? new Date(start) : null;
  const e = end ? new Date(end) : null;
  const fmt = (d: Date) =>
    d.toLocaleDateString("en-GB", {
      weekday: "short",
      day: "numeric",
      month: "short",
    });
  if (s && e && !Number.isNaN(s.getTime()) && !Number.isNaN(e.getTime())) {
    const year = e.getFullYear();
    return `${fmt(s)} – ${fmt(e)} ${year}`;
  }
  const only = (s && !Number.isNaN(s.getTime()) && s) || (e as Date);
  return only ? fmt(only) : null;
}

/* -------------------------------------------------------------------------- */
/* Itinerary derivation (clean labels, dedupe, conflict detection)              */
/* -------------------------------------------------------------------------- */

const CATEGORY_LABEL: Record<string, string> = {
  national_museums: "Museum",
  museums: "Museum",
  museum: "Museum",
  religion: "Religion",
  historic: "Historic",
  palace: "Palace",
  fountain: "Fountain",
  street: "Street",
  milestone: "Milestone",
  culture: "Culture",
  nature: "Nature",
  nightlife: "Nightlife",
  food: "Food",
  tourism: "Sightseeing",
};

function cleanCategory(cat?: string | null): string | null {
  if (!cat) return null;
  const key = cat.toLowerCase().trim();
  if (CATEGORY_LABEL[key]) return CATEGORY_LABEL[key];
  return key
    .replace(/[_-]+/g, " ")
    .replace(/\s+/g, " ")
    .trim()
    .replace(/\b\w/g, (c) => c.toUpperCase());
}

/** Normalised key for dedupe: lowercased, "the" + parentheticals + punctuation stripped. */
function normalizeName(name: string): string {
  return name
    .toLowerCase()
    .replace(/\(.*?\)/g, " ")
    .replace(/\bthe\b/g, " ")
    .replace(/[^a-z0-9]+/g, " ")
    .trim();
}

interface Stop {
  name: string;
  category: string | null;
  env: string;
  start: string | null;
  end: string | null;
  startMin: number | null;
  endMin: number | null;
  lat: number | null;
  lon: number | null;
  rescheduled: boolean;
  hours: string | null;
}

function toStops(activities: ItineraryActivity[]): Stop[] {
  const seen = new Set<string>();
  const stops: Stop[] = [];
  for (const a of activities) {
    const key = normalizeName(a.name);
    if (key && seen.has(key)) continue; // dedupe near-identical stops within the day
    if (key) seen.add(key);
    stops.push({
      name: a.name,
      category: cleanCategory(a.category),
      env: a.indoor_outdoor,
      start: a.start_time ?? null,
      end: a.end_time ?? null,
      startMin: toMin(a.start_time),
      endMin: toMin(a.end_time),
      lat: a.latitude ?? null,
      lon: a.longitude ?? null,
      rescheduled: a.was_rescheduled,
      hours: a.opening_hours ?? null,
    });
  }
  return stops;
}

interface DayModel {
  dayNumber: number;
  stops: Stop[];
  /** An informative note (arrival on day 1, airport transfer on the last day) — not a warning;
   * the engine already schedules the stops around the journey, so these just explain the gap. */
  note: string | null;
}

function buildDays(plan: ChosenPlan): DayModel[] {
  const flight = plan.transport?.flight ?? null;
  const isAir = plan.transport?.mode === "airplane";
  const total = plan.itinerary.length;
  const destCode = flight?.destination.code;

  return plan.itinerary.map((day, idx) => {
    const stops = toStops(day.activities);
    const notes: string[] = [];

    // Day 1: the engine already begins the plan after the traveller arrives — just say so.
    if (isAir && idx === 0 && flight?.outbound?.arrival_time) {
      notes.push(
        `You land at ${destCode ?? "the airport"} at ${flight.outbound.arrival_time}, so the day's plans begin once you've arrived and settled in.`,
      );
    }
    // Last day: the plan wraps up in time to reach the airport for the return.
    if (isAir && idx === total - 1 && flight?.inbound?.depart_time) {
      notes.push(
        `Your return flight leaves ${destCode ?? ""} at ${flight.inbound.depart_time}, so the last stops finish with time to spare for the airport.`,
      );
    }

    return {
      dayNumber: day.day_number,
      stops,
      note: notes.length ? notes.join(" ") : null,
    };
  });
}

/* -------------------------------------------------------------------------- */
/* Small shared pieces                                                          */
/* -------------------------------------------------------------------------- */

function Dot({ color, size = 8 }: { color: string; size?: number }) {
  return (
    <span
      style={{
        width: size,
        height: size,
        borderRadius: size / 2,
        background: color,
        flex: "none",
        display: "inline-block",
      }}
    />
  );
}

function LiveBadge({ text = "Live price" }: { text?: string }) {
  return (
    <span
      style={{
        display: "inline-flex",
        gap: 6,
        alignItems: "center",
        fontSize: 11,
        fontWeight: 600,
        color: "#1f8a55",
      }}
    >
      <Dot color="#22a565" size={6} />
      {text}
    </span>
  );
}

function btnStyle(
  bg: string,
  fg = "#fff",
  border?: string,
): React.CSSProperties {
  return {
    padding: "11px 18px",
    borderRadius: 12,
    background: bg,
    color: fg,
    fontSize: 14,
    fontWeight: 600,
    border: border ?? "none",
    cursor: "pointer",
    fontFamily: "inherit",
    display: "inline-flex",
    alignItems: "center",
    gap: 8,
    textDecoration: "none",
  };
}

/* -------------------------------------------------------------------------- */
/* Page                                                                         */
/* -------------------------------------------------------------------------- */

interface ResultsPageProps {
  response: TripResponse;
  /** The request that produced the plan (budget, preferences, origin) — optional in tests. */
  request?: TripRequestPayload | null;
  onBack?: () => void;
  onPlanAnother?: () => void;
  onCompare?: () => void;
}

export default function ResultsPage({
  response,
  request = null,
  onBack,
  onPlanAnother,
  onCompare,
}: ResultsPageProps) {
  const plan = response.chosen_plan;
  const { destination } = plan;
  const costs = plan.cost_breakdown;
  const total = plan.total_cost;
  const budget = request?.budget ?? null;
  const left = budget != null ? Math.round(budget - total) : null;
  const score = scoreOutOf100(plan.overall_score);

  const nextBest = useMemo(() => {
    const other = response.ranked_candidates.find(
      (c) => c.destination.city !== destination.city,
    );
    return other?.destination.city ?? null;
  }, [response.ranked_candidates, destination.city]);

  const prefs = useMemo(() => {
    const p = request?.preferences ?? {};
    return Object.entries(p)
      .filter(([, w]) => (w ?? 0) > 0)
      .map(([k]) => k);
  }, [request?.preferences]);

  const days = useMemo(() => buildDays(plan), [plan]);
  const sightCount = days.reduce((n, d) => n + d.stops.length, 0);
  const nights = estimateNights(plan) ?? dateSpanNights(plan) ?? days.length;

  const anyEstimated =
    response.meta.degraded_sources.length > 0 ||
    plan.transport?.quality_flag !== "real" ||
    (plan.accommodation != null && plan.accommodation.quality_flag !== "real");

  const photo = useCityPhoto(`${destination.city}`);

  // Journey selection + a "show the stay on the map" spotlight are lifted here so the stay card's
  // "Show on map" button can jump to the journey's day map and open the accommodation pin.
  const [journeySel, setJourneySel] = useState<Selection>(0);
  const [stayFocus, setStayFocus] = useState<MapFocus | null>(null);

  const showAccommodationOnMap = () => {
    const a = plan.accommodation;
    if (a?.latitude != null && a.longitude != null) {
      setJourneySel(0); // ensure a day view (with its map) is showing, not a flight step
      setStayFocus({ lat: a.latitude, lon: a.longitude, nonce: Date.now() });
    }
    document
      .getElementById(JOURNEY_ID)
      ?.scrollIntoView({ behavior: "smooth", block: "start" });
  };

  return (
    <div
      style={{
        color: C.ink,
        fontFamily: "'Plus Jakarta Sans',system-ui,sans-serif",
      }}
      className="animate-enter"
    >
      <div className="flex flex-col gap-6">
        {/* Back bar + estimated chip */}
        <div className="flex items-center justify-between text-[13px]">
          <button
            type="button"
            onClick={onBack}
            style={{ all: "unset", cursor: "pointer", color: C.muted }}
          >
            ← Back to planning
          </button>
          {anyEstimated && (
            <span
              style={{
                display: "inline-flex",
                gap: 8,
                alignItems: "center",
                padding: "5px 11px",
                borderRadius: 99,
                background: C.warnBg,
                color: C.warnInk,
                fontWeight: 500,
              }}
            >
              <Dot color="#e0a100" size={6} />
              Some figures estimated · prices indicative
            </span>
          )}
        </div>

        {/* Hero */}
        <div className="grid items-center gap-8 min-[900px]:grid-cols-[minmax(0,1fr)_380px]">
          <div className="flex flex-col gap-3.5">
            <div className="flex flex-wrap items-center gap-2">
              <span
                style={{
                  padding: "4px 10px",
                  borderRadius: 99,
                  background: C.teal,
                  color: "#fff",
                  fontSize: 12,
                  fontWeight: 600,
                }}
              >
                Top match · {score}/100
              </span>
              {nextBest && (
                <span style={{ fontSize: 13, color: C.muted }}>
                  Next best: {nextBest}
                </span>
              )}
            </div>
            <h1
              style={{
                margin: 0,
                fontSize: 52,
                lineHeight: 1,
                letterSpacing: "-.03em",
                fontWeight: 800,
              }}
              className="!text-4xl min-[900px]:!text-[52px]"
            >
              {destination.city}, {destination.country}
            </h1>
            <div style={{ fontSize: 15, color: C.muted }}>
              {[
                formatDateRange(plan.start_date, plan.end_date),
                `${nights} ${nights === 1 ? "night" : "nights"}`,
                request?.origin_label ? `from ${request.origin_label}` : null,
              ]
                .filter(Boolean)
                .join(" · ")}
            </div>
            <WhySentence prefs={prefs} budget={budget} left={left} />
          </div>
          <HeroPhoto
            imageUrl={photo?.imageUrl ?? null}
            city={destination.city}
          />
        </div>

        {/* Summary strip — the Activities tile only appears when activities actually cost
            something; free sights total €0, so we omit the tile rather than show "€0". */}
        <div
          className={`grid grid-cols-2 gap-3 ${
            costs.activities > 0
              ? "min-[900px]:grid-cols-[1.6fr_1fr_1fr_1fr]"
              : "min-[900px]:grid-cols-[1.6fr_1fr_1fr]"
          }`}
        >
          <div
            style={{ ...CARD, borderRadius: 16, padding: "18px 20px" }}
            className="col-span-2 flex flex-col gap-2.5 min-[900px]:col-span-1"
          >
            <div className="flex items-baseline justify-between">
              <span style={{ fontSize: 12, color: C.muted, fontWeight: 600 }}>
                Estimated total
              </span>
              {budget != null && (
                <span style={{ fontSize: 12, color: C.muted }}>
                  of {euro(budget)}
                </span>
              )}
            </div>
            <div style={{ fontSize: 34, fontWeight: 800, letterSpacing: "-.02em" }}>
              ≈ {euro(total)}
            </div>
            <BudgetBar costs={costs} budget={budget} total={total} />
            {left != null ? (
              <div style={{ fontSize: 12, color: C.tealDark, fontWeight: 600 }}>
                {left >= 0
                  ? `${euro(left)} left for food & extras`
                  : `${euro(Math.abs(left))} over budget`}
              </div>
            ) : (
              <div style={{ fontSize: 12, color: C.muted }}>
                Transport, stay and activities combined
              </div>
            )}
          </div>
          <Stat
            color={C.teal}
            label={TRANSPORT_WORD[plan.transport?.mode ?? "airplane"]}
            value={euro(costs.transport)}
            sub={transportSub(plan.transport)}
          />
          <Stat
            color={C.indigo}
            label="Stay"
            value={euro(costs.accommodation)}
            sub={
              plan.accommodation
                ? `${nights} nights · ${euro(plan.accommodation.price_per_night)} / night`
                : "No stay"
            }
          />
          {costs.activities > 0 && (
            <Stat
              color={C.amber}
              label="Activities"
              value={euro(costs.activities)}
              sub={`${sightCount} sights`}
            />
          )}
        </div>

        {/* Weather */}
        <WeatherStrip weather={plan.weather} startDate={plan.start_date} />

        {/* Flights + stay */}
        <div className="grid gap-4 min-[900px]:grid-cols-2">
          <TransportSummaryCard transport={plan.transport} cost={costs.transport} />
          <StayCard
            accommodation={plan.accommodation}
            nights={nights}
            onShowOnMap={showAccommodationOnMap}
          />
        </div>

        {/* Journey */}
        <Journey
          plan={plan}
          days={days}
          sel={journeySel}
          onSelect={setJourneySel}
          focus={stayFocus}
        />

        {/* Footer actions */}
        <div className="flex flex-wrap items-center justify-between gap-3 px-1 pt-1">
          <span style={{ fontSize: 13, color: C.muted }}>
            {nextBest
              ? `Not quite right? Compare it with ${nextBest} or start over.`
              : "Not quite right? Start over."}
          </span>
          <div className="flex gap-2.5">
            {onCompare && (
              <button
                type="button"
                onClick={onCompare}
                style={btnStyle("#fff", C.ink, "1px solid #d7e2df")}
              >
                Compare destinations
              </button>
            )}
            <button
              type="button"
              onClick={onPlanAnother ?? onBack}
              style={btnStyle(C.ink)}
            >
              Plan another trip
            </button>
          </div>
        </div>
      </div>
    </div>
  );
}

/* -------------------------------------------------------------------------- */
/* Hero bits                                                                    */
/* -------------------------------------------------------------------------- */

function WhySentence({
  prefs,
  budget,
  left,
}: {
  prefs: string[];
  budget: number | null;
  left: number | null;
}) {
  const parts: React.ReactNode[] = [];
  if (prefs.length) {
    parts.push(
      <>
        Matches your <b>{prefs.join(", ")}</b> picks best
      </>,
    );
  }
  if (budget != null && left != null && left >= 0) {
    parts.push(
      <>
        stays{" "}
        <b style={{ color: C.tealDark }}>{euro(left)} under</b> your{" "}
        {euro(budget)} budget
      </>,
    );
  }
  return (
    <p
      style={{
        margin: "4px 0 0",
        fontSize: 16,
        lineHeight: 1.55,
        maxWidth: 560,
      }}
    >
      {parts.length ? (
        <>
          {parts.map((p, i) => (
            <span key={i}>
              {i === 0 ? "" : ", and "}
              {p}
            </span>
          ))}
          .
        </>
      ) : (
        "A strong all-round match for your trip."
      )}
    </p>
  );
}

function HeroPhoto({
  imageUrl,
  city,
}: {
  imageUrl: string | null;
  city: string;
}) {
  return (
    <div
      style={{
        height: 230,
        borderRadius: 18,
        overflow: "hidden",
        background: `linear-gradient(135deg, ${C.tealTint}, #dfe9e7)`,
      }}
      className="hidden min-[900px]:block"
    >
      {imageUrl ? (
        <img
          src={imageUrl}
          alt={city}
          loading="lazy"
          style={{
            width: "100%",
            height: "100%",
            objectFit: "cover",
            display: "block",
          }}
          onError={(e) => {
            e.currentTarget.style.display = "none";
          }}
        />
      ) : (
        <div
          className="flex h-full w-full items-center justify-center"
          style={{ color: C.tealDark, fontSize: 22, fontWeight: 700 }}
        >
          {city}
        </div>
      )}
    </div>
  );
}

/** Fetch a real Wikipedia lead image for the hero (best-effort; null when unavailable). */
function useCityPhoto(city: string): { imageUrl: string | null } | null {
  const [photo, setPhoto] = useState<{ imageUrl: string | null } | null>(null);
  useEffect(() => {
    let active = true;
    setPhoto(null);
    void fetchDestinationSummary(city)
      .then((s) => {
        if (active && s) setPhoto({ imageUrl: s.imageUrl });
      })
      .catch(() => {
        /* best-effort */
      });
    return () => {
      active = false;
    };
  }, [city]);
  return photo;
}

/* -------------------------------------------------------------------------- */
/* Summary strip                                                                */
/* -------------------------------------------------------------------------- */

function Stat({
  color,
  label,
  value,
  sub,
}: {
  color: string;
  label: string;
  value: string;
  sub: string;
}) {
  return (
    <div
      style={{ ...CARD, borderRadius: 16, padding: "18px 20px" }}
      className="flex flex-col gap-1.5"
    >
      <span
        className="flex items-center gap-1.5"
        style={{ fontSize: 12, color: C.muted, fontWeight: 600 }}
      >
        <Dot color={color} />
        {label}
      </span>
      <span style={{ fontSize: 24, fontWeight: 700 }}>{value}</span>
      <span style={{ fontSize: 12, color: C.muted }}>{sub}</span>
    </div>
  );
}

function BudgetBar({
  costs,
  budget,
  total,
}: {
  costs: ChosenPlan["cost_breakdown"];
  budget: number | null;
  total: number;
}) {
  // Scale against the budget when known, else against the total (shows the mix proportionally).
  const scale = budget && budget > 0 ? budget : total || 1;
  const pct = (v: number) => `${Math.max(0, (v / scale) * 100)}%`;
  return (
    <div
      className="flex gap-0.5 overflow-hidden"
      style={{ height: 8, borderRadius: 4, background: "#edf1f0" }}
    >
      <div style={{ width: pct(costs.transport), background: C.teal }} />
      <div style={{ width: pct(costs.accommodation), background: C.indigo }} />
      {costs.activities > 0 && (
        <div style={{ width: pct(costs.activities), background: C.amber }} />
      )}
    </div>
  );
}

function transportSub(transport?: PlanTransport | null): string {
  if (!transport) return "—";
  if (transport.mode === "airplane") {
    const dur =
      transport.flight?.outbound?.duration_min != null
        ? fmtDurMin(transport.flight.outbound.duration_min)
        : null;
    return ["Return", dur].filter(Boolean).join(" · ") || "Return";
  }
  const parts = [`Return by ${transport.mode}`];
  if (transport.distance_km != null) {
    parts.push(`≈ ${Math.round(transport.distance_km).toLocaleString()} km`);
  }
  return parts.join(" · ");
}

/* -------------------------------------------------------------------------- */
/* Weather                                                                      */
/* -------------------------------------------------------------------------- */

function weatherColor(day: PlanWeatherDay): string {
  const text = (day.conditions ?? "").toLowerCase();
  if (/rain|drizzle|shower|storm|thunder/.test(text) || (day.precip_prob ?? 0) >= 50)
    return "#6aa8d8";
  if (/cloud|overcast|fog|mist/.test(text) || (day.precip_prob ?? 0) >= 25)
    return "#c9d3d1";
  return "#f5b301";
}

// The live forecast only reaches ~16 days out; beyond that the backend serves typical
// conditions averaged from recent years, which the UI labels honestly.
const FORECAST_HORIZON_DAYS = 14;

function WeatherStrip({
  weather,
  startDate,
}: {
  weather: PlanWeatherDay[];
  startDate?: string | null;
}) {
  const hasForecast = weather.length > 0;
  const daysOut = startDate
    ? Math.ceil((new Date(startDate).getTime() - Date.now()) / 86400000)
    : null;
  const typical = daysOut != null && daysOut > FORECAST_HORIZON_DAYS;
  return (
    <div
      style={{ ...CARD, padding: "18px 22px" }}
      className="grid items-center gap-6 min-[720px]:grid-cols-[220px_minmax(0,1fr)]"
    >
      <div className="flex flex-col gap-1.5">
        <span style={{ fontSize: 15, fontWeight: 700 }}>Weather</span>
        {hasForecast ? (
          typical ? (
            <>
              <span
                style={{
                  alignSelf: "start",
                  padding: "3px 9px",
                  borderRadius: 7,
                  background: C.warnBg,
                  color: C.warnInk,
                  fontSize: 11,
                  fontWeight: 600,
                }}
              >
                Typical conditions
              </span>
              <span style={{ fontSize: 12, color: C.muted, lineHeight: 1.45 }}>
                Your trip is too far ahead for a live forecast — these are typical
                conditions for the dates, averaged from recent years, so the actual
                weather can differ.
              </span>
            </>
          ) : (
            <LiveBadge text="Live forecast" />
          )
        ) : (
          <span style={{ fontSize: 12, color: C.muted, lineHeight: 1.45 }}>
            A live forecast isn't available for these dates yet — check again
            closer to departure.
          </span>
        )}
      </div>
      {hasForecast ? (
        // A scrollable row of fixed-width day cards: they stretch to fill for a short trip and
        // scroll horizontally for a long one, so a 5-day and a 16-day forecast both read well.
        <div className="-mx-1 overflow-x-auto px-1 pb-1">
          <div className="flex gap-2">
            {weather.map((w, i) => (
              <div
                key={w.forecast_date}
                style={{
                  padding: "12px 12px",
                  borderRadius: 12,
                  background: C.bg,
                }}
                className="flex min-w-[92px] flex-1 flex-col gap-1.5"
              >
                <div className="flex items-center justify-between">
                  <span
                    style={{ fontSize: 12, fontWeight: 600, color: C.muted }}
                  >
                    D{i + 1} · {weekday(w.forecast_date) || dayMonth(w.forecast_date)}
                  </span>
                  <Dot color={weatherColor(w)} size={10} />
                </div>
                <div className="flex items-baseline gap-1.5">
                  <span style={{ fontSize: 22, fontWeight: 700 }}>
                    {w.temp_max != null ? `${Math.round(w.temp_max)}°` : "—"}
                  </span>
                  <span style={{ fontSize: 13, color: C.muted }}>
                    {w.temp_min != null ? `${Math.round(w.temp_min)}°` : ""}
                  </span>
                </div>
                <span
                  className="truncate"
                  style={{ fontSize: 12, color: C.muted }}
                  title={w.conditions ?? undefined}
                >
                  {w.conditions ?? "—"}
                </span>
                {w.precip_prob != null && (
                  <span style={{ fontSize: 11, color: C.muted }}>
                    {Math.round(w.precip_prob)}% rain
                  </span>
                )}
              </div>
            ))}
          </div>
        </div>
      ) : (
        <div
          style={{
            padding: "16px 18px",
            borderRadius: 12,
            background: C.bg,
            color: C.muted,
            fontSize: 13,
          }}
        >
          Typical conditions for your dates will appear here once the forecast
          window opens.
        </div>
      )}
    </div>
  );
}

/* -------------------------------------------------------------------------- */
/* Flights + stay                                                               */
/* -------------------------------------------------------------------------- */

function FlightMiniRow({
  label,
  date,
  dep,
  arr,
  from,
  to,
  dur,
}: {
  label: string;
  date: string;
  dep: string;
  arr: string;
  from: string;
  to: string;
  dur: string;
}) {
  return (
    <div className="grid grid-cols-[64px_1fr] items-center gap-3">
      <span style={{ fontSize: 12, color: C.muted, fontWeight: 600 }}>
        {label}
        <br />
        {date}
      </span>
      <div className="grid grid-cols-[auto_1fr_auto] items-center gap-3.5">
        <div>
          <div style={{ fontSize: 20, fontWeight: 700 }}>{dep}</div>
          <div style={{ fontSize: 12, color: C.muted }}>{from}</div>
        </div>
        <div className="flex flex-col items-center gap-1">
          <span style={{ fontSize: 11, color: C.muted }}>{dur}</span>
          <div
            style={{
              height: 2,
              width: "100%",
              background:
                "repeating-linear-gradient(90deg,#b8cbc7 0 6px,transparent 6px 10px)",
            }}
          />
        </div>
        <div className="text-right">
          <div style={{ fontSize: 20, fontWeight: 700 }}>{arr}</div>
          <div style={{ fontSize: 12, color: C.muted }}>{to}</div>
        </div>
      </div>
    </div>
  );
}

/** The left card: real flight legs for air, else a generic transport summary. */
function TransportSummaryCard({
  transport,
  cost,
}: {
  transport?: PlanTransport | null;
  cost: number;
}) {
  if (!transport) {
    return (
      <div style={{ ...CARD, padding: 22 }} className="flex flex-col gap-3">
        <span style={{ fontSize: 15, fontWeight: 700 }}>Transport</span>
        <p style={{ fontSize: 14, color: C.muted }}>
          No transport details available.
        </p>
      </div>
    );
  }
  const f = transport.flight;
  const Icon = TRANSPORT_ICON[transport.mode];
  const live = transport.quality_flag === "real";

  if (transport.mode === "airplane" && f) {
    return (
      <div style={{ ...CARD, padding: 22 }} className="flex flex-col gap-4">
        <div className="flex items-center justify-between">
          <span style={{ fontSize: 15, fontWeight: 700 }}>Flights</span>
          {live && <LiveBadge />}
        </div>
        {f.outbound && (
          <FlightMiniRow
            label="Out"
            date={dayMonth(f.outbound.date)}
            dep={f.outbound.depart_time ?? "—"}
            arr={f.outbound.arrival_time ?? "—"}
            from={`${f.origin.code} · ${f.origin.city ?? ""}`}
            to={`${f.destination.code} · ${f.destination.city ?? ""}`}
            dur={
              f.outbound.duration_min != null
                ? `${fmtDurMin(f.outbound.duration_min)} · ${stopsLabel(f.outbound.stops)}`
                : stopsLabel(f.outbound.stops)
            }
          />
        )}
        {f.inbound && (
          <>
            <div style={{ height: 1, background: "#edf1f0" }} />
            <FlightMiniRow
              label="Back"
              date={dayMonth(f.inbound.date)}
              dep={f.inbound.depart_time ?? "—"}
              arr={f.inbound.arrival_time ?? "—"}
              from={`${f.destination.code} · ${f.destination.city ?? ""}`}
              to={`${f.origin.code} · ${f.origin.city ?? ""}`}
              dur={
                f.inbound.duration_min != null
                  ? `${fmtDurMin(f.inbound.duration_min)} · ${stopsLabel(f.inbound.stops)}`
                  : stopsLabel(f.inbound.stops)
              }
            />
          </>
        )}
        <div className="mt-auto flex items-center justify-between pt-1.5">
          <div>
            <div style={{ fontSize: 20, fontWeight: 700 }}>{euro(cost)}</div>
            <div style={{ fontSize: 12, color: C.muted }}>total, return</div>
          </div>
          {f.booking_url && (
            <a
              href={f.booking_url}
              target="_blank"
              rel="noopener noreferrer"
              style={btnStyle(C.teal)}
            >
              Reserve flight <IconArrowRight className="h-4 w-4" />
            </a>
          )}
        </div>
      </div>
    );
  }

  // Non-air: a compact transport summary.
  return (
    <div style={{ ...CARD, padding: 22 }} className="flex flex-col gap-4">
      <div className="flex items-center justify-between">
        <span style={{ fontSize: 15, fontWeight: 700 }}>Transport</span>
        {live && <LiveBadge />}
      </div>
      <div className="flex items-center gap-3">
        <span
          className="flex items-center justify-center"
          style={{
            width: 44,
            height: 44,
            borderRadius: 12,
            background: C.tealTint,
            color: C.tealDark,
          }}
        >
          <Icon className="h-6 w-6" />
        </span>
        <div>
          <div
            style={{ fontSize: 18, fontWeight: 700 }}
            className="capitalize"
          >
            {transport.mode}
          </div>
          {transport.distance_km != null && (
            <div style={{ fontSize: 13, color: C.muted }}>
              ≈ {Math.round(transport.distance_km).toLocaleString()} km return
            </div>
          )}
        </div>
      </div>
      <div className="mt-auto flex items-center justify-between pt-1.5">
        <div>
          <div style={{ fontSize: 20, fontWeight: 700 }}>{euro(cost)}</div>
          <div style={{ fontSize: 12, color: C.muted }}>
            total, return
            {transport.duration_min != null
              ? ` · ${fmtDurMin(transport.duration_min)}`
              : ""}
          </div>
        </div>
      </div>
    </div>
  );
}

function stopsLabel(stops?: number | null): string {
  if (stops == null) return "direct";
  if (stops === 0) return "direct";
  return `${stops} ${stops === 1 ? "stop" : "stops"}`;
}

function StayCard({
  accommodation,
  nights,
  onShowOnMap,
}: {
  accommodation?: PlanAccommodation | null;
  nights: number;
  onShowOnMap?: () => void;
}) {
  if (!accommodation) {
    return (
      <div style={{ ...CARD, padding: 22 }} className="flex flex-col gap-3">
        <span style={{ fontSize: 15, fontWeight: 700 }}>Stay</span>
        <p style={{ fontSize: 14, color: C.muted }}>
          No accommodation details available.
        </p>
      </div>
    );
  }
  const a = accommodation;
  const amenities = a.amenities ?? [];
  const total = a.price_per_night * nights;
  const live = a.quality_flag === "real";
  const hasCoords = a.latitude != null && a.longitude != null;

  return (
    <div
      style={{ ...CARD, padding: 22 }}
      className="grid gap-4 min-[520px]:grid-cols-[180px_minmax(0,1fr)]"
    >
      <div
        style={{ borderRadius: 12, overflow: "hidden", background: "#dfe9e7" }}
        className="aspect-[4/3] min-[520px]:aspect-auto"
      >
        {a.picture_url && (
          <img
            src={a.picture_url}
            alt={a.name ?? "Accommodation"}
            loading="lazy"
            style={{
              width: "100%",
              height: "100%",
              objectFit: "cover",
              display: "block",
            }}
            onError={(e) => {
              e.currentTarget.style.display = "none";
            }}
          />
        )}
      </div>
      <div className="flex flex-col gap-2.5">
        <div className="flex items-center justify-between">
          <span style={{ fontSize: 12, color: C.muted, fontWeight: 600 }}>
            Stay{a.neighbourhood ? ` · ${a.neighbourhood}` : ""}
          </span>
          {live && <LiveBadge text="Live" />}
        </div>
        <div style={{ fontSize: 19, fontWeight: 700, lineHeight: 1.2 }}>
          {a.name ?? "Selected stay"}
        </div>
        <div style={{ fontSize: 13, color: C.muted }} className="capitalize">
          {[
            a.accom_type,
            a.accommodates != null ? `sleeps ${a.accommodates}` : null,
          ]
            .filter(Boolean)
            .join(" · ")}
          {a.rating != null && (
            <>
              {" · "}
              <span style={{ color: C.ink, fontWeight: 600 }}>
                ★ {a.rating.toFixed(1)}
              </span>
              {a.review_count != null ? ` (${a.review_count})` : ""}
            </>
          )}
        </div>
        {amenities.length > 0 && (
          <div className="flex flex-wrap gap-1.5">
            {amenities.slice(0, 4).map((am) => (
              <span
                key={am}
                style={{
                  padding: "3px 9px",
                  borderRadius: 7,
                  background: C.soft,
                  fontSize: 12,
                }}
              >
                {am}
              </span>
            ))}
            {amenities.length > 4 && (
              <span
                style={{
                  padding: "3px 9px",
                  borderRadius: 7,
                  background: C.soft,
                  fontSize: 12,
                }}
              >
                +{amenities.length - 4}
              </span>
            )}
          </div>
        )}
        <div className="mt-auto flex items-center justify-between pt-1">
          <div>
            <div style={{ fontSize: 20, fontWeight: 700 }}>{euro(total)}</div>
            <div style={{ fontSize: 12, color: C.muted }}>
              {euro(a.price_per_night)} × {nights} nights
            </div>
          </div>
          <div className="flex flex-wrap items-center justify-end gap-2">
            {hasCoords && onShowOnMap && (
              <button
                type="button"
                onClick={onShowOnMap}
                style={btnStyle("#fff", C.ink, `1px solid ${C.line}`)}
              >
                <IconLocation className="h-4 w-4 text-teal-600" />
                Show on map
              </button>
            )}
            {a.listing_url && (
              <a
                href={a.listing_url}
                target="_blank"
                rel="noopener noreferrer"
                style={btnStyle(C.teal)}
              >
                Reserve accommodation <IconArrowRight className="h-4 w-4" />
              </a>
            )}
          </div>
        </div>
      </div>
    </div>
  );
}

/* -------------------------------------------------------------------------- */
/* Journey (flights + days as one sequence)                                     */
/* -------------------------------------------------------------------------- */

type Selection = number | "out" | "back";

/** DOM id of the journey section, so the stay card's "Show on map" can scroll to its day map. */
const JOURNEY_ID = "trip-journey";

function Journey({
  plan,
  days,
  sel,
  onSelect,
  focus,
}: {
  plan: ChosenPlan;
  days: DayModel[];
  sel: Selection;
  onSelect: (s: Selection) => void;
  focus?: MapFocus | null;
}) {
  const setSel = onSelect;
  const [hov, setHov] = useState<number | null>(null);
  const transport = plan.transport;
  const flight = transport?.flight ?? null;
  const isAir = transport?.mode === "airplane";

  const steps: {
    k: Selection;
    glyph: string;
    title: string;
    sub: string;
  }[] = [
    {
      k: "out",
      glyph: "↗",
      title: isAir ? "Fly out" : "Depart",
      sub: isAir
        ? `${weekday(flight?.outbound?.date)} · ${flight?.outbound?.depart_time ?? ""}`.trim()
        : dayMonth(plan.start_date) || "Day 1",
    },
    ...days.map((d, i) => ({
      k: i as Selection,
      glyph: `D${i + 1}`,
      title: `Day ${i + 1}`,
      sub: `${d.stops.length} ${d.stops.length === 1 ? "stop" : "stops"}`,
    })),
    {
      k: "back",
      glyph: "↘",
      title: isAir ? "Fly home" : "Return",
      sub: isAir
        ? `${weekday(flight?.inbound?.date)} · ${flight?.inbound?.depart_time ?? ""}`.trim()
        : dayMonth(plan.end_date) || `Day ${days.length}`,
    },
  ];

  const isDay = typeof sel === "number";
  const day = isDay ? days[sel] : null;

  return (
    <div id={JOURNEY_ID} className="flex flex-col gap-4 scroll-mt-4">
      <span style={{ fontSize: 18, fontWeight: 700 }}>
        Your trip, step by step
      </span>

      {/* Stepper — scrolls horizontally when it doesn't fit. The padding gives the selected
          step's glow ring and raised label card room inside the scroll box: setting overflow-x
          to auto also makes overflow-y auto (CSS), which would otherwise clip them. */}
      <div className="-mx-2 overflow-x-auto px-2 pb-4 pt-2">
        <div
          className="relative grid gap-2.5"
          style={{
            gridTemplateColumns: `repeat(${steps.length},minmax(104px,1fr))`,
            minWidth: steps.length * 112,
          }}
        >
          <div
            style={{
              position: "absolute",
              left: "4%",
              right: "4%",
              top: 22,
              height: 2,
              background: "#d7e2df",
            }}
          />
          {steps.map((s) => {
            const on = sel === s.k;
            const isFl = typeof s.k === "string";
            return (
              <button
                key={String(s.k)}
                type="button"
                onClick={() => {
                  setSel(s.k);
                  setHov(null);
                }}
                style={{ all: "unset", cursor: "pointer" }}
                className="relative flex flex-col items-center gap-2.5"
              >
                <div
                  style={{
                    position: "relative",
                    zIndex: 1,
                    width: 44,
                    height: 44,
                    borderRadius: 22,
                    display: "grid",
                    placeItems: "center",
                    fontSize: isFl ? 18 : 12,
                    fontWeight: 700,
                    background: on ? C.teal : "#fff",
                    color: on ? "#fff" : isFl ? C.tealDark : C.ink,
                    border: `2px solid ${on ? C.teal : "#d7e2df"}`,
                    boxShadow: on ? "0 0 0 5px rgba(15,148,136,.15)" : "none",
                  }}
                >
                  {s.glyph}
                </div>
                <div
                  className="flex w-full flex-col items-center gap-1 text-center"
                  style={{
                    padding: "8px 6px",
                    borderRadius: 12,
                    boxSizing: "border-box",
                    background: on ? "#fff" : "transparent",
                    border: `1px solid ${on ? C.line : "transparent"}`,
                  }}
                >
                  <span style={{ fontSize: 13, fontWeight: 700 }}>
                    {s.title}
                  </span>
                  <span style={{ fontSize: 11, color: C.muted }}>{s.sub}</span>
                </div>
              </button>
            );
          })}
        </div>
      </div>

      {isDay && day ? (
        <DayView
          plan={plan}
          day={day}
          index={sel as number}
          hov={hov}
          setHov={setHov}
          focus={focus}
        />
      ) : (
        <FlightDetail
          plan={plan}
          leg={sel === "back" ? "inbound" : "outbound"}
        />
      )}
    </div>
  );
}

function DayView({
  plan,
  day,
  index,
  hov,
  setHov,
  focus,
}: {
  plan: ChosenPlan;
  day: DayModel;
  index: number;
  hov: number | null;
  setHov: (n: number | null) => void;
  focus?: MapFocus | null;
}) {
  const markers = useMemo(() => dayMarkers(plan, day), [plan, day]);
  return (
    <div
      style={{ ...CARD, borderRadius: 20, padding: 22 }}
      className="grid gap-5 min-[900px]:grid-cols-[minmax(0,1fr)_minmax(0,460px)]"
    >
      <div className="flex flex-col gap-1">
        <div className="flex items-baseline justify-between pb-2">
          <span style={{ fontSize: 20, fontWeight: 800 }}>
            Day {index + 1}
          </span>
          <span style={{ fontSize: 13, color: C.muted }}>
            {day.stops.length} {day.stops.length === 1 ? "stop" : "stops"}
          </span>
        </div>

        {day.stops.length === 0 ? (
          <p style={{ fontSize: 14, color: C.muted }}>
            A free day — nothing scheduled.
          </p>
        ) : (
          day.stops.map((it, i) => {
            const e = ENV[it.env] ?? ENV.mixed;
            const dur = fmtRange(it.start, it.end);
            return (
              <div
                key={`${it.name}-${i}`}
                onMouseEnter={() => setHov(i + 1)}
                onMouseLeave={() => setHov(null)}
                className="flex items-center gap-3"
                style={{
                  padding: 10,
                  borderRadius: 12,
                  background: hov === i + 1 ? C.tealTint : "transparent",
                  transition: "background .15s",
                }}
              >
                <span
                  style={{
                    fontVariantNumeric: "tabular-nums",
                    fontSize: 12,
                    color: C.muted,
                    width: 46,
                    flex: "none",
                  }}
                >
                  {it.start ?? ""}
                </span>
                <span
                  style={{
                    width: 24,
                    height: 24,
                    borderRadius: 12,
                    background: C.teal,
                    color: "#fff",
                    display: "grid",
                    placeItems: "center",
                    fontSize: 11,
                    fontWeight: 700,
                    flex: "none",
                  }}
                >
                  {i + 1}
                </span>
                <div className="min-w-0 flex-1">
                  <div style={{ fontSize: 14, fontWeight: 600 }}>{it.name}</div>
                  <div style={{ fontSize: 12, color: C.muted }}>
                    {[dur, it.category].filter(Boolean).join(" · ")}
                  </div>
                  {it.hours && (
                    <div
                      className="truncate"
                      style={{ fontSize: 11, color: C.muted }}
                      title={`Opening hours: ${it.hours}`}
                    >
                      🕒 {it.hours}
                    </div>
                  )}
                </div>
                <span
                  style={{
                    padding: "3px 9px",
                    borderRadius: 7,
                    background: e.bg,
                    color: e.fg,
                    fontSize: 11,
                    fontWeight: 600,
                    flex: "none",
                  }}
                >
                  {e.label}
                </span>
              </div>
            );
          })
        )}

        {day.note && (
          <div
            className="mt-1.5 flex gap-3"
            style={{
              padding: "12px 14px",
              borderRadius: 12,
              background: C.tealTint,
              color: "#0d4a44",
              fontSize: 13,
              lineHeight: 1.45,
            }}
          >
            <b>→</b>
            <span>{day.note}</span>
          </div>
        )}
      </div>

      {markers.length > 0 ? (
        <MapView
          markers={markers}
          highlightNumber={hov}
          focus={focus}
          ariaLabel={`Map of day ${index + 1} stops in ${plan.destination.city}`}
          className="h-[320px] w-full overflow-hidden rounded-2xl ring-1 ring-slate-200 min-[900px]:h-full"
        />
      ) : (
        <div
          className="flex items-center justify-center rounded-2xl"
          style={{ background: C.soft, color: C.muted, fontSize: 13, minHeight: 320 }}
        >
          No mapped stops for this day.
        </div>
      )}
    </div>
  );
}

function dayMarkers(plan: ChosenPlan, day: DayModel): MapMarker[] {
  const markers: MapMarker[] = [];
  const a = plan.accommodation;
  if (a?.latitude != null && a.longitude != null) {
    markers.push({
      lat: a.latitude,
      lon: a.longitude,
      label: a.name ?? "Your stay",
      kind: "accommodation",
    });
  }
  day.stops.forEach((s, i) => {
    if (s.lat != null && s.lon != null) {
      markers.push({
        lat: s.lat,
        lon: s.lon,
        label: `${i + 1}. ${s.name}`,
        kind: "activity",
        number: i + 1,
      });
    }
  });
  return markers;
}

function FlightDetail({
  plan,
  leg,
}: {
  plan: ChosenPlan;
  leg: "outbound" | "inbound";
}) {
  const transport = plan.transport;
  const f = transport?.flight ?? null;
  const cost = plan.cost_breakdown.transport;

  if (!f || transport?.mode !== "airplane") {
    const Icon = TRANSPORT_ICON[transport?.mode ?? "car"];
    return (
      <div
        style={{ ...CARD, borderRadius: 20, padding: 28 }}
        className="flex items-center gap-4"
      >
        <span
          className="flex items-center justify-center"
          style={{ width: 48, height: 48, borderRadius: 14, background: C.tealTint, color: C.tealDark }}
        >
          <Icon className="h-6 w-6" />
        </span>
        <div>
          <div style={{ fontSize: 18, fontWeight: 700 }} className="capitalize">
            {leg === "outbound" ? "Departure" : "Return"} by {transport?.mode}
          </div>
          <div style={{ fontSize: 13, color: C.muted }}>
            {euro(cost)} total, return
          </div>
        </div>
      </div>
    );
  }

  const l = leg === "inbound" ? f.inbound : f.outbound;
  const from = leg === "inbound" ? f.destination : f.origin;
  const to = leg === "inbound" ? f.origin : f.destination;

  return (
    <div
      style={{ ...CARD, borderRadius: 20, padding: 28 }}
      className="grid items-center gap-6 min-[900px]:grid-cols-[auto_1fr_auto_240px]"
    >
      <div>
        <div style={{ fontSize: 12, fontWeight: 600, color: C.tealDark }}>
          {leg === "inbound" ? "Return" : "Outbound"} · {dayMonth(l?.date)}
        </div>
        <div style={{ fontSize: 40, fontWeight: 800, letterSpacing: "-.02em" }}>
          {l?.depart_time ?? "—"}
        </div>
        <div style={{ fontSize: 13, color: C.muted }}>
          {from.code} · {from.city ?? from.airport ?? ""}
        </div>
      </div>
      <div className="flex flex-col items-center gap-1.5">
        <span style={{ fontSize: 12, color: C.muted }}>
          {l?.duration_min != null ? fmtDurMin(l.duration_min) : ""} ·{" "}
          {stopsLabel(l?.stops)}
        </span>
        <div
          style={{
            height: 2,
            width: "100%",
            background:
              "repeating-linear-gradient(90deg,#b8cbc7 0 6px,transparent 6px 10px)",
          }}
        />
      </div>
      <div className="text-right">
        <div style={{ fontSize: 12, color: C.muted }}>arrive</div>
        <div style={{ fontSize: 40, fontWeight: 800, letterSpacing: "-.02em" }}>
          {l?.arrival_time ?? "—"}
        </div>
        <div style={{ fontSize: 13, color: C.muted }}>
          {to.code} · {to.city ?? to.airport ?? ""}
        </div>
      </div>
      <div
        style={{ padding: 16, borderRadius: 14, background: C.bg }}
        className="flex flex-col gap-2.5"
      >
        <div style={{ fontSize: 13, color: C.muted }}>Return flights total</div>
        <div style={{ fontSize: 24, fontWeight: 800 }}>{euro(cost)}</div>
        {f.booking_url && (
          <a
            href={f.booking_url}
            target="_blank"
            rel="noopener noreferrer"
            style={{ ...btnStyle(C.teal), justifyContent: "center" }}
          >
            Reserve flights <IconArrowRight className="h-4 w-4" />
          </a>
        )}
      </div>
    </div>
  );
}

/* -------------------------------------------------------------------------- */
/* Derivations                                                                  */
/* -------------------------------------------------------------------------- */

/** Nights from the accommodation spend ÷ nightly price (null when not derivable). */
function estimateNights(plan: ChosenPlan): number | null {
  const a = plan.accommodation;
  if (!a || a.price_per_night <= 0) return null;
  const nights = Math.round(plan.cost_breakdown.accommodation / a.price_per_night);
  return nights > 0 ? nights : null;
}

/** Nights from the trip window dates (fallback when the price-based figure isn't available). */
function dateSpanNights(plan: ChosenPlan): number | null {
  if (!plan.start_date || !plan.end_date) return null;
  const s = new Date(plan.start_date);
  const e = new Date(plan.end_date);
  if (Number.isNaN(s.getTime()) || Number.isNaN(e.getTime())) return null;
  const nights = Math.round((e.getTime() - s.getTime()) / 86400000);
  return nights > 0 ? nights : null;
}
