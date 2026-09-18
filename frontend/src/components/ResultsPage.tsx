/**
 * ResultsPage — renders the chosen plan of a {@link TripResponse} (BS-085).
 *
 * Shows every `chosen_plan` field: destination, match score, cost breakdown (with budget
 * utilization), transport, accommodation, a day-by-day itinerary (indoor/outdoor tags +
 * rescheduled markers), and a weather summary (with weather-conflict flags). The optional
 * top-level `explanation` renders as "Why {city} won". Purely presentational — it takes a
 * response and an optional `onBack` handler, so it renders straight from a fixture in tests.
 */
import type {
  ChosenPlan,
  ItineraryActivity,
  PlanWeatherDay,
  TripResponse,
} from "../api/types";

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

const INDOOR_OUTDOOR_LABEL: Record<string, string> = {
  indoor: "Indoor",
  outdoor: "Outdoor",
  mixed: "Indoor / outdoor",
};

interface ResultsPageProps {
  response: TripResponse;
  onBack?: () => void;
}

export default function ResultsPage({ response, onBack }: ResultsPageProps) {
  const plan = response.chosen_plan;
  const { destination } = plan;

  return (
    <div className="mx-auto max-w-4xl space-y-8">
      {/* Page header */}
      <header className="flex flex-wrap items-center justify-between gap-4">
        <div>
          <h1 className="text-3xl font-extrabold tracking-tight text-slate-800">
            Your winning itinerary
          </h1>
          <p className="mt-1 text-sm text-slate-500">
            {plan.itinerary.length > 0
              ? `${plan.itinerary.length}-day trip`
              : "Trip plan"}{" "}
            · {destination.city}, {destination.country}
          </p>
        </div>
        {onBack && (
          <button
            type="button"
            onClick={onBack}
            className="rounded-xl border border-slate-200 bg-white px-4 py-2 text-sm font-medium text-slate-700 shadow-sm hover:border-slate-300"
          >
            ← Back to planning
          </button>
        )}
      </header>

      <HeroCard plan={plan} />

      <div className="grid gap-8 lg:grid-cols-2">
        <CostBreakdownCard plan={plan} />
        <div className="space-y-8">
          <TransportCard plan={plan} />
          <AccommodationCard plan={plan} />
        </div>
      </div>

      <WeatherCard weather={plan.weather} />
      <ItineraryCard plan={plan} />

      {response.explanation && (
        <section className="rounded-2xl bg-teal-50/70 p-6 ring-1 ring-teal-100">
          <h2 className="text-lg font-bold text-slate-800">
            Why {destination.city} won
          </h2>
          <p className="mt-2 whitespace-pre-line text-sm leading-relaxed text-slate-700">
            {response.explanation}
          </p>
        </section>
      )}
    </div>
  );
}

function Card({
  title,
  children,
  aside,
}: {
  title: string;
  children: React.ReactNode;
  aside?: React.ReactNode;
}) {
  return (
    <section className="rounded-2xl bg-white p-6 shadow-sm ring-1 ring-slate-200">
      <div className="mb-4 flex items-center justify-between gap-3">
        <h2 className="text-lg font-bold text-slate-800">{title}</h2>
        {aside}
      </div>
      {children}
    </section>
  );
}

function HeroCard({ plan }: { plan: ChosenPlan }) {
  const { destination } = plan;
  return (
    <section className="overflow-hidden rounded-2xl bg-white shadow-sm ring-1 ring-slate-200">
      <div className="flex flex-col gap-6 p-6 sm:flex-row sm:items-center sm:justify-between">
        <div>
          <span className="inline-flex items-center gap-1 rounded-full bg-amber-100 px-3 py-1 text-xs font-semibold text-amber-800">
            ⭐ Winner · {destination.country}
          </span>
          <h2 className="mt-3 text-4xl font-extrabold tracking-tight text-slate-900">
            {destination.city}
          </h2>
          <p className="mt-1 text-sm text-slate-500">
            {destination.latitude.toFixed(3)},{" "}
            {destination.longitude.toFixed(3)}
          </p>
        </div>
        <div className="flex gap-8">
          <div>
            <p className="text-xs uppercase tracking-wide text-slate-400">
              Match score
            </p>
            <p className="text-3xl font-extrabold text-teal-600">
              {scoreOutOf100(plan.overall_score)}
              <span className="text-lg font-semibold text-slate-400">/100</span>
            </p>
          </div>
          <div className="border-l border-slate-200 pl-8">
            <p className="text-xs uppercase tracking-wide text-slate-400">
              Estimated total
            </p>
            <p className="text-3xl font-extrabold text-slate-900">
              {euro(plan.total_cost)}
            </p>
          </div>
        </div>
      </div>
    </section>
  );
}

const COST_ROWS: {
  key: keyof ChosenPlan["cost_breakdown"];
  label: string;
  icon: string;
}[] = [
  { key: "transport", label: "Transport", icon: "🚗" },
  { key: "accommodation", label: "Accommodation", icon: "🏨" },
  { key: "activities", label: "Activities", icon: "🎟️" },
];

function CostBreakdownCard({ plan }: { plan: ChosenPlan }) {
  const { cost_breakdown, total_cost } = plan;
  return (
    <Card title="Cost breakdown">
      <table className="w-full text-sm">
        <thead>
          <tr className="text-left text-xs uppercase tracking-wide text-slate-400">
            <th className="pb-2 font-medium">Category</th>
            <th className="pb-2 text-right font-medium">Amount</th>
          </tr>
        </thead>
        <tbody className="divide-y divide-slate-100">
          {COST_ROWS.map((row) => (
            <tr key={row.key}>
              <td className="py-3 text-slate-700">
                <span className="mr-2" aria-hidden>
                  {row.icon}
                </span>
                {row.label}
              </td>
              <td className="py-3 text-right font-semibold tabular-nums text-slate-900">
                {euro(cost_breakdown[row.key])}
              </td>
            </tr>
          ))}
          <tr className="border-t-2 border-slate-200">
            <td className="py-3 font-bold text-slate-800">Total</td>
            <td className="py-3 text-right font-extrabold tabular-nums text-slate-900">
              {euro(total_cost)}
            </td>
          </tr>
        </tbody>
      </table>
    </Card>
  );
}

function DetailRow({
  label,
  value,
}: {
  label: string;
  value: React.ReactNode;
}) {
  return (
    <div className="flex items-center justify-between gap-4 py-1.5">
      <dt className="text-sm text-slate-500">{label}</dt>
      <dd className="text-sm font-semibold text-slate-800">{value}</dd>
    </div>
  );
}

function TransportCard({ plan }: { plan: ChosenPlan }) {
  const t = plan.transport;
  if (!t) {
    return (
      <Card title="Transport">
        <p className="text-sm text-slate-500">
          No transport details available.
        </p>
      </Card>
    );
  }
  return (
    <Card title="Transport" aside={<QualityBadge flag={t.quality_flag} />}>
      <dl>
        <DetailRow
          label="Mode"
          value={<span className="capitalize">{t.mode}</span>}
        />
        {t.cost != null && <DetailRow label="Cost" value={euro(t.cost)} />}
        {t.distance_km != null && (
          <DetailRow
            label="Distance"
            value={`${Math.round(t.distance_km)} km`}
          />
        )}
        {t.duration_min != null && (
          <DetailRow label="Duration" value={formatDuration(t.duration_min)} />
        )}
      </dl>
    </Card>
  );
}

function AccommodationCard({ plan }: { plan: ChosenPlan }) {
  const a = plan.accommodation;
  if (!a) {
    return (
      <Card title="Accommodation">
        <p className="text-sm text-slate-500">
          No accommodation details available.
        </p>
      </Card>
    );
  }
  return (
    <Card title="Accommodation" aside={<QualityBadge flag={a.quality_flag} />}>
      <dl>
        {a.name && <DetailRow label="Name" value={a.name} />}
        {a.accom_type && (
          <DetailRow
            label="Type"
            value={<span className="capitalize">{a.accom_type}</span>}
          />
        )}
        <DetailRow label="Per night" value={euro(a.price_per_night)} />
        {a.rating != null && (
          <DetailRow label="Rating" value={`★ ${a.rating.toFixed(1)}`} />
        )}
      </dl>
    </Card>
  );
}

function WeatherCard({ weather }: { weather: PlanWeatherDay[] }) {
  if (weather.length === 0) {
    return (
      <Card title="Weather summary">
        <p className="text-sm text-slate-500">No forecast available.</p>
      </Card>
    );
  }
  return (
    <Card title="Weather summary">
      <ul className="grid gap-3 sm:grid-cols-2 lg:grid-cols-3">
        {weather.map((day) => (
          <li
            key={day.forecast_date}
            className={`rounded-xl border px-4 py-3 ${
              day.weather_conflict
                ? "border-amber-200 bg-amber-50"
                : "border-slate-200 bg-slate-50"
            }`}
          >
            <div className="flex items-center justify-between">
              <p className="text-sm font-semibold text-slate-800">
                {formatDate(day.forecast_date)}
              </p>
              {day.weather_conflict && (
                <span className="rounded-full bg-amber-100 px-2 py-0.5 text-[11px] font-semibold text-amber-800">
                  ⚠ Rain risk
                </span>
              )}
            </div>
            <p className="mt-1 text-sm text-slate-600">
              {day.conditions ?? "—"}
            </p>
            <p className="mt-1 text-xs text-slate-500">
              {formatTempRange(day)}
              {day.precip_prob != null &&
                ` · ${Math.round(day.precip_prob * 100)}% precip`}
            </p>
          </li>
        ))}
      </ul>
    </Card>
  );
}

function ItineraryCard({ plan }: { plan: ChosenPlan }) {
  if (plan.itinerary.length === 0) {
    return (
      <Card title="Itinerary">
        <p className="text-sm text-slate-500">No itinerary available.</p>
      </Card>
    );
  }
  return (
    <Card title="Day-by-day itinerary">
      <ol className="space-y-6">
        {plan.itinerary.map((day) => (
          <li key={day.day_number}>
            <div className="flex items-center gap-3">
              <span className="flex h-8 w-8 items-center justify-center rounded-full bg-teal-600 text-sm font-bold text-white">
                {day.day_number}
              </span>
              <h3 className="text-sm font-bold uppercase tracking-wide text-slate-700">
                Day {day.day_number}
              </h3>
            </div>
            {day.activities.length === 0 ? (
              <p className="ml-11 mt-2 text-sm text-slate-400">
                A free day — nothing scheduled.
              </p>
            ) : (
              <ul className="ml-11 mt-2 space-y-2">
                {day.activities.map((activity, i) => (
                  <ActivityRow
                    key={`${activity.name}-${i}`}
                    activity={activity}
                  />
                ))}
              </ul>
            )}
          </li>
        ))}
      </ol>
    </Card>
  );
}

function ActivityRow({ activity }: { activity: ItineraryActivity }) {
  const time =
    activity.start_time && activity.end_time
      ? `${activity.start_time}–${activity.end_time}`
      : (activity.start_time ?? null);
  return (
    <li className="flex flex-wrap items-center gap-2 rounded-lg border border-slate-100 bg-slate-50 px-3 py-2">
      {time && (
        <span className="text-xs font-medium tabular-nums text-slate-500">
          {time}
        </span>
      )}
      <span className="text-sm font-medium text-slate-800">
        {activity.name}
      </span>
      {activity.category && (
        <span className="text-xs text-slate-400">· {activity.category}</span>
      )}
      <span className="ml-auto flex items-center gap-2">
        <span
          className={`rounded-full px-2 py-0.5 text-[11px] font-medium ${
            activity.indoor_outdoor === "indoor"
              ? "bg-sky-100 text-sky-700"
              : activity.indoor_outdoor === "outdoor"
                ? "bg-emerald-100 text-emerald-700"
                : "bg-slate-200 text-slate-600"
          }`}
        >
          {INDOOR_OUTDOOR_LABEL[activity.indoor_outdoor] ??
            activity.indoor_outdoor}
        </span>
        {activity.was_rescheduled && (
          <span className="rounded-full bg-amber-100 px-2 py-0.5 text-[11px] font-semibold text-amber-800">
            ↻ Rescheduled
          </span>
        )}
      </span>
    </li>
  );
}

function QualityBadge({ flag }: { flag: string }) {
  const styles: Record<string, string> = {
    real: "bg-emerald-100 text-emerald-700",
    cached: "bg-slate-100 text-slate-600",
    estimated: "bg-amber-100 text-amber-800",
  };
  return (
    <span
      className={`rounded-full px-2.5 py-0.5 text-[11px] font-medium capitalize ${
        styles[flag] ?? "bg-slate-100 text-slate-600"
      }`}
      title="Data quality"
    >
      {flag}
    </span>
  );
}

function formatDuration(minutes: number): string {
  const h = Math.floor(minutes / 60);
  const m = Math.round(minutes % 60);
  if (h === 0) return `${m} min`;
  return m === 0 ? `${h} h` : `${h} h ${m} min`;
}

function formatDate(iso: string): string {
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return iso;
  return d.toLocaleDateString("en-GB", {
    weekday: "short",
    day: "numeric",
    month: "short",
  });
}

function formatTempRange(day: PlanWeatherDay): string {
  const { temp_min, temp_max } = day;
  if (temp_min != null && temp_max != null)
    return `${Math.round(temp_min)}–${Math.round(temp_max)}°C`;
  if (temp_max != null) return `${Math.round(temp_max)}°C`;
  if (temp_min != null) return `${Math.round(temp_min)}°C`;
  return "—";
}
