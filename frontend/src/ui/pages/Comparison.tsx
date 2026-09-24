/**
 * ComparisonPage — the destination evaluation surface (BS-097).
 *
 * Renders every {@link RankedCandidate} from a response side by side across all documented
 * columns (cost, overall score, transport/accommodation cost, the three factor scores, and
 * feasibility). This is an evaluation surface, so **infeasible candidates are flagged, never
 * hidden**, and the chosen destination (the one matching `chosen_plan.destination`) is
 * highlighted with a "Recommended" badge and an elevated teal-framed card.
 *
 * Purely presentational — it takes a response and an optional `onBack`, so it renders straight
 * from a fixture in tests. Candidate order is preserved from the response (the ranking), never
 * re-sorted here.
 */
import type { ComponentType, ReactNode, SVGProps } from "react";

import type {
  CandidateDestination,
  RankedCandidate,
  TripResponse,
} from "@/types/trip";
import {
  IconBed,
  IconCar,
  IconCheck,
  IconCloudSun,
  IconGauge,
  IconHeart,
  IconInfo,
  IconStar,
  IconWallet,
  IconX,
} from "@/ui/components/common/Icons";

function euro(value: number): string {
  return new Intl.NumberFormat("en-IE", {
    style: "currency",
    currency: "EUR",
    maximumFractionDigits: 0,
  }).format(value);
}

function scoreOutOf100(score: number): number {
  return Math.round(Math.max(0, Math.min(1, score)) * 100);
}

function sameDestination(
  a: CandidateDestination,
  b: CandidateDestination,
): boolean {
  return (
    a.city.trim().toLowerCase() === b.city.trim().toLowerCase() &&
    a.country.trim().toLowerCase() === b.country.trim().toLowerCase()
  );
}

type IconType = ComponentType<SVGProps<SVGSVGElement>>;

const ROW_H = "h-16";

/** The value rows, in the documented order, each with its label icon and sub-label. */
const VALUE_ROWS: {
  key: string;
  label: string;
  sublabel?: string;
  Icon: IconType;
  tint: "teal" | "amber";
  render: (c: RankedCandidate, isChosen: boolean) => ReactNode;
}[] = [
  {
    key: "total_cost",
    label: "Total cost",
    sublabel: "estimated",
    Icon: IconWallet,
    tint: "teal",
    render: (c) => <Money value={c.total_cost} approx bold />,
  },
  {
    key: "overall_score",
    label: "Overall score",
    sublabel: "/100",
    Icon: IconGauge,
    tint: "teal",
    render: (c, isChosen) => (
      <OverallScore value={c.overall_score} highlight={isChosen} />
    ),
  },
  {
    key: "transport_cost",
    label: "Transport cost",
    sublabel: "estimated",
    Icon: IconCar,
    tint: "teal",
    render: (c) => <Money value={c.transport_cost} />,
  },
  {
    key: "accommodation_cost",
    label: "Accommodation cost",
    sublabel: "estimated",
    Icon: IconBed,
    tint: "teal",
    render: (c) => <Money value={c.accommodation_cost} />,
  },
  {
    key: "experience_score",
    label: "Experience score",
    sublabel: "/100",
    Icon: IconStar,
    tint: "amber",
    render: (c) => <FactorBar value={c.experience_score} />,
  },
  {
    key: "weather_score",
    label: "Weather score",
    sublabel: "/100",
    Icon: IconCloudSun,
    tint: "amber",
    render: (c) => <FactorBar value={c.weather_score} />,
  },
  {
    key: "preference_score",
    label: "Preference score",
    sublabel: "/100",
    Icon: IconHeart,
    tint: "amber",
    render: (c) => <FactorBar value={c.preference_score} />,
  },
  {
    key: "feasible",
    label: "Feasible within budget",
    Icon: IconCheck,
    tint: "teal",
    render: (c) => <FeasibilityBadge feasible={c.feasible} />,
  },
];

interface ComparisonPageProps {
  response: TripResponse;
  onBack?: () => void;
}

export default function ComparisonPage({
  response,
  onBack,
}: ComparisonPageProps) {
  const candidates = response.ranked_candidates;
  const chosen = response.chosen_plan.destination;

  return (
    <div className="animate-enter space-y-6">
      <header className="flex flex-wrap items-end justify-between gap-4">
        <div>
          <h1 className="text-3xl font-extrabold tracking-tight text-slate-900">
            Top destination comparisons
          </h1>
          <p className="mt-2 text-sm text-slate-500">
            How the best options compare against your priorities and budget.
          </p>
        </div>
        {onBack && (
          <button
            type="button"
            onClick={onBack}
            className="rounded-xl border border-slate-200 bg-white px-4 py-2 text-sm font-medium text-slate-700 shadow-sm hover:border-slate-300"
          >
            ← Back to results
          </button>
        )}
      </header>

      {candidates.length === 0 ? (
        <div className="rounded-2xl bg-white p-10 text-center text-sm text-slate-500 ring-1 ring-slate-200">
          No candidate destinations to compare.
        </div>
      ) : (
        <>
          {/* Columns flex to fill the width when there are only a few candidates, and fall
              back to horizontal scroll (via each column's min-width) when there are many. */}
          <div className="overflow-x-auto px-1 pb-2 pt-1">
            <div className="flex gap-3">
              <LabelColumn />
              {candidates.map((candidate, i) => (
                <CandidateColumn
                  key={`${candidate.destination.city}-${candidate.destination.country}-${i}`}
                  candidate={candidate}
                  isChosen={sameDestination(candidate.destination, chosen)}
                  index={i}
                />
              ))}
            </div>
          </div>

          <div className="flex items-center justify-between gap-3">
            <p className="flex items-center gap-2 text-xs text-slate-400">
              <IconInfo className="h-4 w-4" />
              All figures are estimated and based on current data.
            </p>
            <span className="rounded-full border border-slate-200 bg-white px-3 py-1 text-xs font-medium text-slate-400">
              estimated
            </span>
          </div>
        </>
      )}
    </div>
  );
}

/**
 * The left-hand row labels — icon chip + label + sub-label. Mirrors the candidate columns'
 * top spacing and row heights so every row lines up across the table.
 */
function LabelColumn() {
  return (
    <div className="w-56 shrink-0">
      {/* Spacer matching the candidate cards' top padding + chip + header block, so rows align. */}
      <div className="h-[7.25rem]" />
      {VALUE_ROWS.map((row) => (
        <div key={row.key} className={`flex ${ROW_H} items-center gap-3`}>
          <span
            className={`flex h-9 w-9 shrink-0 items-center justify-center rounded-xl ${
              row.tint === "teal"
                ? "bg-teal-50 text-teal-600"
                : "bg-amber-50 text-amber-600"
            }`}
          >
            <row.Icon className="h-[18px] w-[18px]" />
          </span>
          <div className="leading-tight">
            <p className="text-sm font-medium text-slate-700">{row.label}</p>
            {row.sublabel && (
              <p className="text-xs text-slate-400">({row.sublabel})</p>
            )}
          </div>
        </div>
      ))}
    </div>
  );
}

function CandidateColumn({
  candidate,
  isChosen,
  index,
}: {
  candidate: RankedCandidate;
  isChosen: boolean;
  index: number;
}) {
  const { destination } = candidate;
  return (
    <div
      className={`animate-enter min-w-[11rem] flex-1 rounded-2xl bg-white px-4 pb-6 pt-5 transition duration-200 hover:-translate-y-1 hover:shadow-xl ${
        isChosen
          ? "shadow-xl ring-2 ring-teal-400"
          : "shadow-sm ring-1 ring-slate-200"
      }`}
      style={{ "--enter-delay": `${index * 70}ms` } as React.CSSProperties}
      data-testid={`candidate-${destination.city}`}
    >
      {/* Top chip: the Recommended badge (chosen) or a subtle marker (others).
          Kept inside the card so it renders in full within the scroll container. */}
      <div className="flex h-8 items-center justify-center">
        {isChosen ? (
          <span className="inline-flex items-center gap-1 rounded-full bg-amber-400 px-3 py-1 text-xs font-semibold text-white shadow-sm">
            <IconStar className="h-3.5 w-3.5" />
            Recommended
          </span>
        ) : (
          <span className="flex h-6 w-6 items-center justify-center rounded-full bg-slate-50 text-slate-300 ring-1 ring-slate-200">
            <IconStar className="h-3.5 w-3.5" />
          </span>
        )}
      </div>

      {/* Destination header. */}
      <div className="mt-2 flex h-14 flex-col items-center justify-center text-center">
        <p className="truncate text-lg font-extrabold text-slate-900">
          {destination.city}
        </p>
        <p className="truncate text-sm text-slate-500">{destination.country}</p>
      </div>

      {VALUE_ROWS.map((row) => (
        <div
          key={row.key}
          className={`flex ${ROW_H} items-center justify-center`}
        >
          {row.render(candidate, isChosen)}
        </div>
      ))}
    </div>
  );
}

function Money({
  value,
  approx = false,
  bold = false,
}: {
  value?: number | null;
  approx?: boolean;
  bold?: boolean;
}) {
  if (value == null) return <Dash />;
  return (
    <span
      className={`text-lg tabular-nums text-slate-900 ${
        bold ? "font-extrabold" : "font-semibold"
      }`}
    >
      {approx ? "≈ " : ""}
      {euro(value)}
    </span>
  );
}

function OverallScore({
  value,
  highlight,
}: {
  value?: number | null;
  highlight: boolean;
}) {
  if (value == null) return <Dash />;
  return (
    <span
      className={`text-2xl font-extrabold tabular-nums ${
        highlight ? "text-teal-600" : "text-slate-800"
      }`}
    >
      {scoreOutOf100(value)}
    </span>
  );
}

function FactorBar({ value }: { value?: number | null }) {
  if (value == null) return <Dash />;
  const pct = Math.round(Math.max(0, Math.min(1, value)) * 100);
  return (
    <div className="w-full px-1">
      <p className="mb-1.5 text-center text-xs font-semibold tabular-nums text-slate-600">
        {value.toFixed(2)}
      </p>
      <div className="h-2 w-full overflow-hidden rounded-full bg-slate-100">
        <div
          className="h-2 rounded-full bg-gradient-to-r from-teal-600 to-teal-300"
          style={{ width: `${pct}%` }}
        />
      </div>
    </div>
  );
}

function FeasibilityBadge({ feasible }: { feasible: boolean }) {
  return feasible ? (
    <span className="inline-flex items-center gap-1.5 rounded-full bg-emerald-100 px-3 py-1.5 text-sm font-semibold text-emerald-700">
      <IconCheck className="h-4 w-4" />
      Yes
    </span>
  ) : (
    <span className="inline-flex items-center gap-1.5 rounded-full bg-rose-100 px-3 py-1.5 text-sm font-semibold text-rose-700">
      <IconX className="h-4 w-4" />
      No
    </span>
  );
}

function Dash() {
  return <span className="text-lg text-slate-300">—</span>;
}
