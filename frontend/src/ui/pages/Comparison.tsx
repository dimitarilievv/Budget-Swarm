/**
 * ComparisonPage — "Compare destinations" (design 3a: city cards).
 *
 * Renders every {@link RankedCandidate} as a sortable row of city cards (by overall score,
 * #1 marked "Recommended"), each with a score ring, a budget-aware cost box, and a factor
 * list. This is an evaluation surface, so infeasible candidates are shown (never hidden) and
 * clearly marked — a missing stay price yields "€{transport}+", a hatched bar segment, and a
 * "Find a stay" button rather than a fabricated total.
 *
 * Purely presentational: it takes the response plus the originating request (for budget, dates
 * and origin — which the response omits) and renders straight from fixtures in tests.
 */
import { useMemo, useState } from "react";

import type {
  CandidateDestination,
  RankedCandidate,
  TripResponse,
  TripRequestPayload,
} from "@/types/trip";

const C = {
  teal: "#0f9488",
  tealDark: "#0b7a70",
  tealTint: "#e6f5f2",
  indigo: "#6366f1",
  ink: "#12201e",
  muted: "#5d6b69",
  line: "#e4ebe9",
  soft: "#f4f8f7",
  okInk: "#1f8a55",
  warnBg: "#fff6e0",
  warnInk: "#8a5a00",
};
const HATCH = "repeating-linear-gradient(135deg,#f0d9a0 0 4px,#fff6e0 4px 8px)";

const pct = (v?: number | null): number =>
  v == null ? 0 : Math.round(Math.max(0, Math.min(1, v)) * 100);

function euro(value: number): string {
  return new Intl.NumberFormat("en-IE", {
    style: "currency",
    currency: "EUR",
    maximumFractionDigits: 0,
  }).format(value);
}

function sameDestination(a: CandidateDestination, b: CandidateDestination): boolean {
  return (
    a.city.trim().toLowerCase() === b.city.trim().toLowerCase() &&
    a.country.trim().toLowerCase() === b.country.trim().toLowerCase()
  );
}

function formatDateRange(start?: string | null, end?: string | null): string | null {
  if (!start && !end) return null;
  const fmt = (iso: string) => {
    const d = new Date(iso);
    return Number.isNaN(d.getTime())
      ? null
      : d.toLocaleDateString("en-GB", { day: "numeric", month: "short" });
  };
  const s = start ? fmt(start) : null;
  const e = end ? fmt(end) : null;
  if (s && e) return `${s} – ${e}`;
  return s ?? e;
}

interface ComparisonPageProps {
  response: TripResponse;
  /** The request that produced the plan (budget, origin) — optional in tests. */
  request?: TripRequestPayload | null;
  onBack?: () => void;
  /** Act on a card: "View trip" (recommended), "Plan this" (fits), or "Find a stay". */
  onAction?: (city: CandidateDestination, label: string) => void;
}

export default function ComparisonPage({
  response,
  request = null,
  onBack,
  onAction,
}: ComparisonPageProps) {
  const chosen = response.chosen_plan.destination;
  const budget = request?.budget ?? null;

  const cities = useMemo(
    () =>
      [...response.ranked_candidates].sort(
        (a, b) => (b.overall_score ?? 0) - (a.overall_score ?? 0),
      ),
    [response.ranked_candidates],
  );
  // The recommended card is the destination we actually planned (falls back to the top score).
  const recommendedIndex = useMemo(() => {
    const i = cities.findIndex((c) => sameDestination(c.destination, chosen));
    return i >= 0 ? i : 0;
  }, [cities, chosen]);

  const [selected, setSelected] = useState(recommendedIndex);

  const estimated =
    response.meta.degraded_sources.length > 0 ||
    (response.chosen_plan.transport != null &&
      response.chosen_plan.transport.quality_flag !== "real") ||
    (response.chosen_plan.accommodation != null &&
      response.chosen_plan.accommodation.quality_flag !== "real");

  const dates = formatDateRange(
    response.chosen_plan.start_date,
    response.chosen_plan.end_date,
  );
  const recommendedCity = cities[recommendedIndex]?.destination.city ?? chosen.city;

  const subline = [
    dates,
    request?.origin_label ? `from ${request.origin_label}` : null,
    budget != null ? `${euro(budget)} budget` : null,
  ]
    .filter(Boolean)
    .join(" · ");

  return (
    <div
      className="animate-enter"
      style={{ color: C.ink, fontFamily: "'Plus Jakarta Sans',system-ui,sans-serif" }}
    >
      <div className="flex flex-col gap-6">
        {/* Header */}
        <div className="flex flex-wrap items-end justify-between gap-5">
          <div className="flex flex-col gap-1.5">
            {onBack && (
              <button
                type="button"
                onClick={onBack}
                style={{ all: "unset", cursor: "pointer", fontSize: 13, color: C.muted }}
              >
                ← Back to {recommendedCity}
              </button>
            )}
            <h1 style={{ margin: 0, fontSize: 34, letterSpacing: "-.03em", fontWeight: 800 }}>
              Compare destinations
            </h1>
            {subline && (
              <span style={{ fontSize: 15, color: C.muted }}>{subline}</span>
            )}
          </div>
          {estimated && (
            <span
              style={{
                padding: "6px 12px",
                borderRadius: 99,
                background: C.warnBg,
                color: C.warnInk,
                fontSize: 12,
                fontWeight: 600,
              }}
            >
              All prices estimated
            </span>
          )}
        </div>

        {cities.length === 0 ? (
          <div
            className="rounded-2xl bg-white p-10 text-center text-sm"
            style={{ color: C.muted, border: `1px solid ${C.line}` }}
          >
            No candidate destinations to compare.
          </div>
        ) : (
          <>
            <div
              className="grid items-stretch gap-3.5 pt-1"
              style={{ gridTemplateColumns: "repeat(auto-fit,minmax(200px,1fr))" }}
            >
              {cities.map((city, i) => (
                <CityCard
                  key={`${city.destination.city}-${city.destination.country}-${i}`}
                  city={city}
                  rank={i + 1}
                  budget={budget}
                  active={selected === i}
                  recommended={i === recommendedIndex}
                  onSelect={() => setSelected(i)}
                  onAction={onAction}
                />
              ))}
            </div>

            <div
              className="flex flex-wrap gap-4"
              style={{ fontSize: 12, color: C.muted }}
            >
              <LegendDot color={C.teal} label="Transport" />
              <LegendDot color={C.indigo} label="Stay" />
              <LegendDot hatch label="Stay price missing" />
            </div>
          </>
        )}
      </div>
    </div>
  );
}

function CityCard({
  city,
  rank,
  budget,
  active,
  recommended,
  onSelect,
  onAction,
}: {
  city: RankedCandidate;
  rank: number;
  budget: number | null;
  active: boolean;
  recommended: boolean;
  onSelect: () => void;
  onAction?: (city: CandidateDestination, label: string) => void;
}) {
  const transport = city.transport_cost ?? null;
  const stay = city.accommodation_cost ?? null;
  const total = transport != null && stay != null ? transport + stay : null;
  const fits =
    total != null && budget != null
      ? total <= budget
      : total != null
        ? city.feasible
        : false;

  const note =
    total == null
      ? "stay price missing"
      : budget == null
        ? "total for transport + stay"
        : total <= budget
          ? `${euro(budget - total)} under budget`
          : `${euro(total - budget)} over budget`;

  const cta = recommended ? "View trip" : fits ? "Plan this" : "Find a stay";

  const rows: [string, string, boolean][] = [
    ["Transport", transport != null ? euro(transport) : "Not found", transport == null],
    ["Stay", stay != null ? euro(stay) : "Not found", stay == null],
    ["Preferences", String(pct(city.preference_score)), false],
    ["Weather", String(pct(city.weather_score)), false],
  ];

  const score = pct(city.overall_score);
  const budgetLabel =
    budget == null ? null : budget >= 1000 ? `${budget / 1000}k` : String(budget);

  return (
    <div
      role="button"
      tabIndex={0}
      onClick={onSelect}
      onKeyDown={(e) => e.key === "Enter" && onSelect()}
      data-testid={`city-${city.destination.city}`}
      className="flex cursor-pointer flex-col gap-4 outline-none"
      style={{
        position: "relative",
        padding: "22px 18px 18px",
        borderRadius: 22,
        background: "#fff",
        border: `2px solid ${active ? C.teal : "transparent"}`,
        boxShadow: active
          ? "0 14px 32px -12px rgba(15,148,136,.35)"
          : "0 1px 2px rgba(0,0,0,.05)",
        transform: active ? "translateY(-6px)" : "none",
        transition: "all .2s",
      }}
    >
      <div
        className="flex items-center justify-between"
        style={{ minHeight: 20 }}
      >
        <span style={{ fontSize: 12, fontWeight: 700, color: C.muted }}>#{rank}</span>
        {recommended && (
          <span
            style={{
              padding: "3px 9px",
              borderRadius: 99,
              background: C.teal,
              color: "#fff",
              fontSize: 11,
              fontWeight: 700,
            }}
          >
            Recommended
          </span>
        )}
      </div>

      <div className="flex flex-col items-center gap-2.5">
        <ScoreRing value={score} active={active} />
        <div className="text-center">
          <div style={{ fontSize: 20, fontWeight: 800 }}>{city.destination.city}</div>
          <div style={{ fontSize: 12, color: C.muted }}>{city.destination.country}</div>
        </div>
      </div>

      {/* Cost box */}
      <div
        className="flex flex-col gap-1.5"
        style={{ padding: 12, borderRadius: 14, background: C.soft }}
      >
        <div className="flex items-baseline justify-between gap-1.5">
          <span style={{ fontSize: 20, fontWeight: 800, whiteSpace: "nowrap" }}>
            {total != null ? euro(total) : `${euro(transport ?? 0)}+`}
          </span>
          {budgetLabel && (
            <span style={{ fontSize: 11, color: C.muted, whiteSpace: "nowrap" }}>
              of €{budgetLabel}
            </span>
          )}
        </div>
        <div
          className="flex overflow-hidden"
          style={{ gap: 2, height: 7, borderRadius: 4, background: "#e3eae8" }}
        >
          <div
            style={{
              width: `${Math.min(100, ((transport ?? 0) / (budget || total || 1)) * 100)}%`,
              background: C.teal,
            }}
          />
          <div
            style={
              stay != null
                ? {
                    width: `${Math.min(100, (stay / (budget || total || 1)) * 100)}%`,
                    background: C.indigo,
                  }
                : { width: "22%", background: HATCH }
            }
          />
        </div>
        <span
          style={{ fontSize: 12, fontWeight: 600, color: fits ? C.okInk : C.warnInk }}
        >
          {note}
        </span>
      </div>

      {/* Factor list (Experience omitted — it's identical for every city). */}
      <div className="flex flex-col">
        {rows.map(([label, value, missing]) => (
          <div
            key={label}
            className="flex justify-between"
            style={{
              padding: "7px 2px",
              borderBottom: `1px dashed ${C.line}`,
              fontSize: 13,
            }}
          >
            <span style={{ color: C.muted }}>{label}</span>
            <span style={{ fontWeight: 700, color: missing ? C.warnInk : C.ink }}>
              {value}
            </span>
          </div>
        ))}
      </div>

      <button
        type="button"
        onClick={(e) => {
          e.stopPropagation();
          onAction?.(city.destination, cta);
        }}
        className="mt-auto"
        style={{
          padding: 10,
          borderRadius: 12,
          fontSize: 13,
          fontWeight: 700,
          border: "none",
          cursor: "pointer",
          fontFamily: "inherit",
          background: active ? C.teal : "#f1f5f4",
          color: active ? "#fff" : C.ink,
        }}
      >
        {cta}
      </button>
    </div>
  );
}

function ScoreRing({ value, active }: { value: number; active: boolean }) {
  return (
    <div
      style={{
        width: 84,
        height: 84,
        borderRadius: 42,
        display: "grid",
        placeItems: "center",
        background: `conic-gradient(${active ? C.teal : "#5fb8ae"} ${value * 3.6}deg, #e8eeec 0)`,
      }}
    >
      <div
        className="flex flex-col items-center justify-center"
        style={{ width: 66, height: 66, borderRadius: 33, background: "#fff" }}
      >
        <span style={{ fontSize: 22, fontWeight: 800, lineHeight: 1 }}>{value}</span>
        <span
          style={{ fontSize: 9, color: C.muted, fontWeight: 600, letterSpacing: ".04em" }}
        >
          MATCH
        </span>
      </div>
    </div>
  );
}

function LegendDot({
  color,
  hatch,
  label,
}: {
  color?: string;
  hatch?: boolean;
  label: string;
}) {
  return (
    <span className="flex items-center gap-1.5" style={{ whiteSpace: "nowrap" }}>
      <span
        style={{
          width: hatch ? 12 : 8,
          height: 8,
          borderRadius: hatch ? 2 : 4,
          background: hatch ? HATCH : color,
          display: "inline-block",
        }}
      />
      {label}
    </span>
  );
}
