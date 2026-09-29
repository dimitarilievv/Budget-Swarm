/**
 * ResultsPage — renders the chosen plan of a {@link TripResponse} (BS-085, redesigned).
 *
 * A high-fidelity result view: a header with the recommended destination and match score, a
 * summary card row (total cost with a category bar, transport, accommodation with a mini map,
 * and a multi-day weather forecast), a day-by-day itinerary timeline beside an overview map,
 * and the LLM "Why {city}?" explanation with next-step actions.
 *
 * Purely presentational — it takes a response and an optional `onBack` handler, so it renders
 * straight from a fixture in tests. Maps only ever plot markers the response carries real
 * coordinates for (destination centre, accommodation); activity pins are not shown because the
 * itinerary carries no per-activity coordinates.
 */
import type {
  ChosenPlan,
  CostBreakdown,
  ItineraryActivity,
  PlanAccommodation,
  PlanTransport,
  PlanWeatherDay,
  TransportMode,
  TripResponse,
} from "@/types/trip";
import DestinationInfo from "@/ui/components/common/DestinationInfo";
import MapView from "@/ui/components/common/MapView";
import {
  MARKER_COLOR,
  type MapMarker,
  type MarkerKind,
} from "@/ui/components/common/mapMarkers";
import {
  IconArrowRight,
  IconBus,
  IconCar,
  IconCloud,
  IconCloudRain,
  IconCloudSun,
  IconCompare,
  IconInfo,
  IconLocation,
  IconPlane,
  IconSun,
  IconTrain,
} from "@/ui/components/common/Icons";

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
  /** Open the destination comparison; omitted when there are no candidates to compare. */
  onCompare?: () => void;
}

export default function ResultsPage({
  response,
  onBack,
  onCompare,
}: ResultsPageProps) {
  const plan = response.chosen_plan;
  const { destination } = plan;
  const mapMarkers = planMarkers(plan);

  return (
    <div className="animate-enter space-y-6">
      <Header plan={plan} onBack={onBack} />

      <DataQualityNotice degradedSources={response.meta.degraded_sources} />

      <DestinationInfo city={destination.city} />

      {/* Summary card row — cost, transport, accommodation, weather. */}
      <div className="grid gap-5 lg:grid-cols-4 sm:grid-cols-2">
        <TotalCostCard plan={plan} />
        <TransportCard transport={plan.transport} />
        <AccommodationCard
          accommodation={plan.accommodation}
          nights={estimateNights(plan)}
        />
        <WeatherCard weather={plan.weather} />
      </div>

      {/* Itinerary timeline beside the overview map. */}
      <div className="grid gap-5 lg:grid-cols-[minmax(0,1fr)_minmax(0,1fr)]">
        <ItineraryCard plan={plan} />
        <OverviewMapCard markers={mapMarkers} city={destination.city} />
      </div>

      <WhyCard
        city={destination.city}
        explanation={response.explanation}
        onBack={onBack}
        onCompare={onCompare}
      />
    </div>
  );
}

/* -------------------------------------------------------------------------- */
/* Header                                                                      */
/* -------------------------------------------------------------------------- */

function Header({ plan, onBack }: { plan: ChosenPlan; onBack?: () => void }) {
  const { destination } = plan;
  const score = scoreOutOf100(plan.overall_score);
  return (
    <header className="flex flex-wrap items-end justify-between gap-4">
      <div>
        <span className="inline-flex items-center gap-1.5 rounded-full bg-amber-100 px-3 py-1 text-xs font-semibold text-amber-800 ring-1 ring-amber-200/70">
          ★ Recommended
        </span>
        <h1 className="mt-3 text-3xl font-extrabold tracking-tight text-slate-900 sm:text-4xl">
          {destination.city}, {destination.country}
        </h1>
        <p className="mt-2 flex items-center gap-2 text-sm text-slate-500">
          Match score
          <span className="text-2xl font-extrabold text-teal-600">
            {score}
            <span className="text-base font-semibold text-slate-400">/100</span>
          </span>
          <span
            title="How well this destination fits your budget and preferences (0–100)."
            className="text-slate-400"
          >
            <IconInfo className="h-4 w-4" />
          </span>
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
  );
}

/* -------------------------------------------------------------------------- */
/* Card shell                                                                  */
/* -------------------------------------------------------------------------- */

function Card({
  title,
  eyebrow,
  children,
  className = "",
}: {
  title: string;
  eyebrow?: React.ReactNode;
  children: React.ReactNode;
  className?: string;
}) {
  return (
    <section
      className={`flex flex-col rounded-2xl bg-white p-5 shadow-sm ring-1 ring-slate-200 transition-shadow duration-200 hover:shadow-md ${className}`}
    >
      <div className="mb-3 flex items-center justify-between gap-2">
        <h2 className="text-xs font-semibold uppercase tracking-wide text-slate-400">
          {title}
        </h2>
        {eyebrow}
      </div>
      {children}
    </section>
  );
}

/* -------------------------------------------------------------------------- */
/* Total cost                                                                  */
/* -------------------------------------------------------------------------- */

const COST_ROWS: {
  key: keyof CostBreakdown;
  label: string;
  color: string;
}[] = [
  { key: "transport", label: "Transport", color: "#14b8a6" }, // teal-500
  { key: "accommodation", label: "Accommodation", color: "#6366f1" }, // indigo-500
  { key: "activities", label: "Activities", color: "#f59e0b" }, // amber-500
];

function TotalCostCard({ plan }: { plan: ChosenPlan }) {
  const { cost_breakdown, total_cost } = plan;
  const barTotal =
    cost_breakdown.transport +
    cost_breakdown.accommodation +
    cost_breakdown.activities;

  return (
    <Card title="Total cost">
      <p className="text-3xl font-extrabold tracking-tight text-slate-900">
        ≈ {euro(total_cost)}
      </p>

      {/* Stacked category bar. */}
      <div className="mt-4 flex h-2.5 w-full overflow-hidden rounded-full bg-slate-100">
        {barTotal > 0 &&
          COST_ROWS.map((row) => {
            const pct = (cost_breakdown[row.key] / barTotal) * 100;
            if (pct <= 0) return null;
            return (
              <span
                key={row.key}
                style={{ width: `${pct}%`, backgroundColor: row.color }}
                title={`${row.label}: ${euro(cost_breakdown[row.key])}`}
              />
            );
          })}
      </div>

      <dl className="mt-4 space-y-1.5">
        {COST_ROWS.map((row) => (
          <div
            key={row.key}
            className="flex items-center justify-between gap-2 text-sm"
          >
            <dt className="flex items-center gap-2 text-slate-500">
              <span
                className="h-2.5 w-2.5 rounded-full"
                style={{ backgroundColor: row.color }}
                aria-hidden
              />
              {row.label}
            </dt>
            <dd className="font-semibold tabular-nums text-slate-800">
              {euro(cost_breakdown[row.key])}
            </dd>
          </div>
        ))}
      </dl>
    </Card>
  );
}

/* -------------------------------------------------------------------------- */
/* Transport                                                                   */
/* -------------------------------------------------------------------------- */

const TRANSPORT_ICON: Record<
  TransportMode,
  (props: { className?: string }) => JSX.Element
> = {
  airplane: IconPlane,
  car: IconCar,
  bus: IconBus,
  train: IconTrain,
};

function TransportCard({ transport }: { transport?: PlanTransport | null }) {
  if (!transport) {
    return (
      <Card title="Transport">
        <p className="text-sm text-slate-400">
          No transport details available.
        </p>
      </Card>
    );
  }
  const Icon = TRANSPORT_ICON[transport.mode] ?? IconCar;
  return (
    <Card
      title="Transport"
      eyebrow={<QualityBadge flag={transport.quality_flag} />}
    >
      <div className="flex items-center gap-3">
        <span className="flex h-11 w-11 items-center justify-center rounded-xl bg-teal-50 text-teal-600">
          <Icon className="h-6 w-6" />
        </span>
        <div>
          <p className="text-lg font-bold capitalize text-slate-900">
            {transport.mode}
          </p>
          {transport.distance_km != null && (
            <p className="text-sm text-slate-500">
              ≈ {Math.round(transport.distance_km).toLocaleString()} km
            </p>
          )}
        </div>
      </div>
      <dl className="mt-4 space-y-1.5 text-sm">
        {transport.cost != null && (
          <MiniRow label="Cost" value={euro(transport.cost)} />
        )}
        {transport.duration_min != null && (
          <MiniRow
            label="Duration"
            value={formatDuration(transport.duration_min)}
          />
        )}
      </dl>
    </Card>
  );
}

/* -------------------------------------------------------------------------- */
/* Accommodation                                                               */
/* -------------------------------------------------------------------------- */

function AccommodationCard({
  accommodation,
  nights,
}: {
  accommodation?: PlanAccommodation | null;
  nights: number | null;
}) {
  if (!accommodation) {
    return (
      <Card title="Accommodation">
        <p className="text-sm text-slate-400">
          No accommodation details available.
        </p>
      </Card>
    );
  }
  const a = accommodation;
  const hasCoords = a.latitude != null && a.longitude != null;
  const nightsTotal = nights != null ? a.price_per_night * nights : null;

  return (
    <Card
      title="Accommodation"
      eyebrow={<QualityBadge flag={a.quality_flag} />}
    >
      <p className="text-base font-bold leading-tight text-slate-900">
        {a.name ?? "Selected stay"}
      </p>
      {a.accom_type && (
        <p className="text-xs capitalize text-slate-500">{a.accom_type}</p>
      )}
      <p className="mt-2 text-sm text-slate-600">
        {euro(a.price_per_night)}/night
        {nights != null && nightsTotal != null && (
          <>
            {" · "}
            {nights} {nights === 1 ? "night" : "nights"} ={" "}
            <span className="font-semibold text-slate-800">
              {euro(nightsTotal)}
            </span>
          </>
        )}
      </p>
      {a.rating != null && (
        <p className="mt-1 text-sm font-medium text-amber-600">
          ★ {a.rating.toFixed(1)}
        </p>
      )}
      {hasCoords && (
        <MapView
          markers={[
            {
              lat: a.latitude as number,
              lon: a.longitude as number,
              label: a.name ?? "Accommodation",
              kind: "accommodation",
            },
          ]}
          ariaLabel={`Map showing ${a.name ?? "the accommodation"} location`}
          singleZoom={14}
          className="mt-3 h-28 w-full overflow-hidden rounded-xl ring-1 ring-slate-200"
        />
      )}
    </Card>
  );
}

/* -------------------------------------------------------------------------- */
/* Weather                                                                     */
/* -------------------------------------------------------------------------- */

// precip_prob is a percentage in [0, 100] (api.md), matching Open-Meteo's
// precipitation_probability_max — not a 0-1 fraction.
function weatherIcon(day: PlanWeatherDay) {
  const text = (day.conditions ?? "").toLowerCase();
  const rainy =
    /rain|drizzle|shower|storm|thunder/.test(text) ||
    (day.precip_prob != null && day.precip_prob >= 50);
  if (rainy) return IconCloudRain;
  if (/cloud|overcast|fog|mist/.test(text)) return IconCloud;
  if (/part|sun.*cloud|cloud.*sun/.test(text)) return IconCloudSun;
  if (/clear|sun|fair/.test(text)) return IconSun;
  if (day.precip_prob != null && day.precip_prob >= 20) return IconCloudSun;
  return IconSun;
}

/** A day counts as wet enough to surface its precipitation chance and a rain icon. */
function isWetDay(day: PlanWeatherDay): boolean {
  return (
    day.weather_conflict ||
    (day.precip_prob != null && day.precip_prob >= 30) ||
    /rain|drizzle|shower|storm|thunder/.test(
      (day.conditions ?? "").toLowerCase(),
    )
  );
}

/** A short headline + subtitle summarising the whole forecast (top of the card). */
function weatherSummary(weather: PlanWeatherDay[]) {
  const highs = weather
    .map((d) => d.temp_max)
    .filter((n): n is number => n != null);
  const avgHigh = highs.length
    ? highs.reduce((a, b) => a + b, 0) / highs.length
    : null;
  const adjective =
    avgHigh == null
      ? "Mixed"
      : avgHigh < 8
        ? "Cold"
        : avgHigh < 16
          ? "Cool"
          : avgHigh < 24
            ? "Mild"
            : avgHigh < 30
              ? "Warm"
              : "Hot";

  const wet = weather.filter(isWetDay).length;
  const cloudy = weather.filter((d) =>
    /cloud|overcast|fog|mist/.test((d.conditions ?? "").toLowerCase()),
  ).length;
  let sky: "rainy" | "cloudy" | "clear";
  let Icon = IconSun;
  if (wet >= Math.ceil(weather.length / 2)) {
    sky = "rainy";
    Icon = IconCloudRain;
  } else if (cloudy >= weather.length - cloudy) {
    sky = "cloudy";
    Icon = IconCloud;
  } else {
    sky = "clear";
    Icon = IconSun;
  }

  let subtitle = "";
  if (highs.length) {
    const lo = Math.round(Math.min(...highs));
    const hi = Math.round(Math.max(...highs));
    subtitle = lo === hi ? `${hi}° days` : `${lo}–${hi}° days`;
  }
  const firstWet = weather.find(isWetDay);
  if (firstWet) {
    subtitle +=
      (subtitle ? " · " : "") +
      `rain likely ${formatWeekday(firstWet.forecast_date)}`;
  }
  return { title: `${adjective} and ${sky}`, subtitle, Icon };
}

/** One day's low→high temperature bar, positioned within the week's range. */
function TempBar({
  day,
  scaleMin,
  span,
}: {
  day: PlanWeatherDay;
  scaleMin: number;
  span: number;
}) {
  const lo = day.temp_min ?? day.temp_max;
  const hi = day.temp_max ?? day.temp_min;
  if (lo == null || hi == null) {
    return <div className="h-1.5 flex-1 rounded-full bg-slate-100" />;
  }
  const width = Math.max(((hi - lo) / span) * 100, 8); // keep a tiny range visible
  let left = ((lo - scaleMin) / span) * 100;
  if (left + width > 100) left = 100 - width;
  return (
    <div className="relative h-1.5 flex-1 rounded-full bg-slate-200">
      <div
        className="absolute inset-y-0 rounded-full bg-teal-500"
        style={{ left: `${left}%`, width: `${width}%` }}
      />
    </div>
  );
}

function WeatherCard({ weather }: { weather: PlanWeatherDay[] }) {
  if (weather.length === 0) {
    return (
      <Card title="Weather forecast">
        <p className="text-sm text-slate-400">No forecast available.</p>
      </Card>
    );
  }
  const temps = weather.flatMap((d) =>
    [d.temp_min, d.temp_max].filter((n): n is number => n != null),
  );
  const scaleMin = temps.length ? Math.min(...temps) : 0;
  const span = (temps.length ? Math.max(...temps) : 1) - scaleMin || 1;
  const { title, subtitle, Icon: SummaryIcon } = weatherSummary(weather);

  return (
    <Card title={`Weather · ${weather.length}-day forecast`}>
      <div className="mb-2 flex items-center gap-3">
        <SummaryIcon className="h-8 w-8 shrink-0 text-slate-400" />
        <div className="leading-tight">
          <p className="text-sm font-semibold text-slate-700">{title}</p>
          {subtitle && <p className="text-xs text-slate-400">{subtitle}</p>}
        </div>
      </div>
      <ul className="divide-y divide-slate-100">
        {weather.map((day) => {
          const Icon = weatherIcon(day);
          const wet = isWetDay(day);
          return (
            <li
              key={day.forecast_date}
              className="flex items-center gap-3 py-2.5"
              title={day.conditions ?? undefined}
            >
              <div className="w-11 shrink-0 leading-tight">
                <div className="text-sm font-semibold text-slate-700">
                  {formatWeekday(day.forecast_date)}
                </div>
                <div className="text-[11px] text-slate-400">
                  {formatMonthDay(day.forecast_date)}
                </div>
              </div>
              <div className="flex w-8 shrink-0 flex-col items-center">
                <Icon
                  className={`h-5 w-5 ${wet ? "text-sky-500" : "text-slate-400"}`}
                />
                {wet && day.precip_prob != null && (
                  <span className="text-[10px] font-semibold tabular-nums text-sky-500">
                    {Math.round(day.precip_prob)}%
                  </span>
                )}
              </div>
              <span className="w-7 shrink-0 text-right text-xs tabular-nums text-slate-400">
                {day.temp_min != null ? `${Math.round(day.temp_min)}°` : "—"}
              </span>
              <TempBar day={day} scaleMin={scaleMin} span={span} />
              <span className="w-8 shrink-0 text-right text-sm font-semibold tabular-nums text-slate-700">
                {day.temp_max != null ? `${Math.round(day.temp_max)}°` : "—"}
              </span>
            </li>
          );
        })}
      </ul>
    </Card>
  );
}

/* -------------------------------------------------------------------------- */
/* Itinerary                                                                    */
/* -------------------------------------------------------------------------- */

function ItineraryCard({ plan }: { plan: ChosenPlan }) {
  if (plan.itinerary.length === 0) {
    return (
      <Card title="Daily itinerary">
        <p className="text-sm text-slate-400">No itinerary available.</p>
      </Card>
    );
  }
  return (
    <Card title="Daily itinerary">
      <ol className="space-y-5">
        {plan.itinerary.map((day) => {
          const rescheduled = day.activities.some((a) => a.was_rescheduled);
          return (
            <li key={day.day_number} className="flex gap-3">
              <div className="flex flex-col items-center">
                <span className="flex h-9 w-9 shrink-0 items-center justify-center rounded-full bg-teal-600 text-xs font-bold text-white">
                  D{day.day_number}
                </span>
                <span className="mt-1 w-px flex-1 bg-slate-200" aria-hidden />
              </div>
              <div
                className={`min-w-0 flex-1 rounded-xl p-2 ${
                  rescheduled ? "bg-amber-50/60 ring-1 ring-amber-100" : ""
                }`}
              >
                <h3 className="mb-1.5 text-sm font-bold text-slate-700">
                  Day {day.day_number}
                </h3>
                {day.activities.length === 0 ? (
                  <p className="text-sm text-slate-400">
                    A free day — nothing scheduled.
                  </p>
                ) : (
                  <ul className="space-y-1">
                    {day.activities.map((activity, i) => (
                      <ActivityRow
                        key={`${activity.name}-${i}`}
                        activity={activity}
                      />
                    ))}
                  </ul>
                )}
              </div>
            </li>
          );
        })}
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
    <li className="flex flex-wrap items-center gap-x-2 gap-y-1 py-1">
      {time && (
        <span className="w-24 shrink-0 text-xs font-medium tabular-nums text-slate-500">
          {time}
        </span>
      )}
      <span className="text-sm font-medium text-slate-800">
        {activity.name}
      </span>
      {activity.category && (
        <span className="text-xs text-slate-400">· {activity.category}</span>
      )}
      <span className="ml-auto flex items-center gap-1.5">
        <IndoorOutdoorTag value={activity.indoor_outdoor} />
        {activity.was_rescheduled && (
          <span className="rounded-full bg-amber-100 px-2 py-0.5 text-[10px] font-semibold text-amber-800">
            ↻ Rescheduled
          </span>
        )}
      </span>
    </li>
  );
}

function IndoorOutdoorTag({ value }: { value: string }) {
  const style =
    value === "indoor"
      ? "bg-sky-100 text-sky-700"
      : value === "outdoor"
        ? "bg-emerald-100 text-emerald-700"
        : "bg-slate-200 text-slate-600";
  return (
    <span
      className={`rounded-full px-2 py-0.5 text-[10px] font-medium ${style}`}
    >
      {INDOOR_OUTDOOR_LABEL[value] ?? value}
    </span>
  );
}

/* -------------------------------------------------------------------------- */
/* Overview map                                                                 */
/* -------------------------------------------------------------------------- */

const MARKER_LABEL: Record<MarkerKind, string> = {
  destination: "City centre",
  accommodation: "Accommodation",
  activity: "Activities",
  airport: "Airport",
};

function OverviewMapCard({
  markers,
  city,
}: {
  markers: MapMarker[];
  city: string;
}) {
  const kinds = Array.from(new Set(markers.map((m) => m.kind)));
  return (
    <Card title="Trip map" className="min-h-[20rem]">
      {markers.length === 0 ? (
        <div className="flex flex-1 items-center justify-center rounded-xl bg-slate-50 text-sm text-slate-400">
          No mapped locations available.
        </div>
      ) : (
        <>
          <MapView
            markers={markers}
            ariaLabel={`Map of ${city} showing the destination and accommodation`}
            className="min-h-[15rem] flex-1 overflow-hidden rounded-xl ring-1 ring-slate-200"
          />
          <ul className="mt-3 flex flex-wrap gap-x-4 gap-y-1">
            {kinds.map((kind) => (
              <li
                key={kind}
                className="flex items-center gap-1.5 text-xs text-slate-500"
              >
                <span
                  className="h-2.5 w-2.5 rounded-full"
                  style={{ backgroundColor: MARKER_COLOR[kind] }}
                  aria-hidden
                />
                {MARKER_LABEL[kind]}
              </li>
            ))}
          </ul>
        </>
      )}
    </Card>
  );
}

/* -------------------------------------------------------------------------- */
/* Why {city}?                                                                  */
/* -------------------------------------------------------------------------- */

function WhyCard({
  city,
  explanation,
  onBack,
  onCompare,
}: {
  city: string;
  explanation?: string | null;
  onBack?: () => void;
  onCompare?: () => void;
}) {
  return (
    <section className="rounded-2xl bg-teal-50/70 p-6 ring-1 ring-teal-100">
      <div className="flex flex-wrap items-start justify-between gap-4">
        <div className="min-w-0 flex-1">
          <h2 className="flex items-center gap-2 text-lg font-bold text-slate-800">
            <IconLocation className="h-5 w-5 text-teal-600" />
            Why {city}?
          </h2>
          {explanation ? (
            <p className="mt-2 whitespace-pre-line text-sm leading-relaxed text-slate-700">
              {explanation}
            </p>
          ) : (
            <p className="mt-2 text-sm text-slate-500">
              This destination scored highest across your budget, transport and
              preference criteria.
            </p>
          )}
        </div>
        <div className="flex shrink-0 flex-wrap items-center gap-3">
          {onCompare && (
            <button
              type="button"
              onClick={onCompare}
              className="inline-flex items-center gap-2 rounded-xl border border-teal-200 bg-white px-4 py-2.5 text-sm font-semibold text-teal-700 shadow-sm hover:border-teal-300"
            >
              <IconCompare className="h-4 w-4" />
              Compare destinations
            </button>
          )}
          {onBack && (
            <button
              type="button"
              onClick={onBack}
              className="inline-flex items-center gap-2 rounded-xl bg-teal-600 px-4 py-2.5 text-sm font-semibold text-white shadow-sm hover:bg-teal-700"
            >
              Plan another trip
              <IconArrowRight className="h-4 w-4" />
            </button>
          )}
        </div>
      </div>
    </section>
  );
}

/* -------------------------------------------------------------------------- */
/* Small pieces + helpers                                                       */
/* -------------------------------------------------------------------------- */

function MiniRow({ label, value }: { label: string; value: React.ReactNode }) {
  return (
    <div className="flex items-center justify-between gap-2">
      <dt className="text-slate-500">{label}</dt>
      <dd className="font-semibold text-slate-800">{value}</dd>
    </div>
  );
}

const QUALITY_META: Record<
  string,
  { label: string; cls: string; dot: string; title: string }
> = {
  real: {
    label: "Live",
    cls: "bg-emerald-100 text-emerald-700",
    dot: "bg-emerald-500",
    title: "Live data from a real source",
  },
  cached: {
    label: "Cached",
    cls: "bg-slate-100 text-slate-600",
    dot: "bg-slate-400",
    title: "Cached from an earlier fetch",
  },
  estimated: {
    label: "Estimated",
    cls: "bg-amber-100 text-amber-800",
    dot: "bg-amber-500",
    title: "Estimated figure — not a live quote",
  },
};

function QualityBadge({ flag }: { flag: string }) {
  const meta = QUALITY_META[flag] ?? {
    label: flag,
    cls: "bg-slate-100 text-slate-600",
    dot: "bg-slate-400",
    title: "Data quality",
  };
  return (
    <span
      className={`inline-flex items-center gap-1 rounded-full px-2 py-0.5 text-[10px] font-semibold ${meta.cls}`}
      title={meta.title}
    >
      <span className={`h-1.5 w-1.5 rounded-full ${meta.dot}`} aria-hidden />
      {meta.label}
    </span>
  );
}

/**
 * Surfaces `meta.degraded_sources` (BS-088). When the engine reports a source as estimated,
 * cached or unavailable, we say so plainly rather than presenting the plan as fully live — the
 * per-field {@link QualityBadge} marks *which* figures, this banner explains the run as a whole.
 * Renders nothing when every source was healthy.
 */
function DataQualityNotice({ degradedSources }: { degradedSources: string[] }) {
  if (degradedSources.length === 0) return null;
  return (
    <section
      className="flex flex-wrap items-start gap-3 rounded-2xl bg-amber-50 p-4 ring-1 ring-amber-200"
      aria-label="Data quality notice"
    >
      <IconInfo className="mt-0.5 h-5 w-5 shrink-0 text-amber-600" />
      <div className="min-w-0 flex-1">
        <p className="text-sm font-semibold text-amber-900">
          Some figures are estimated or cached
        </p>
        <p className="mt-0.5 text-sm text-amber-800">
          The following sources were degraded or unavailable for this plan, so
          the numbers above are indicative rather than live quotes:
        </p>
        <ul className="mt-2 flex flex-wrap gap-1.5">
          {degradedSources.map((source) => (
            <li
              key={source}
              className="rounded-full bg-amber-100 px-2.5 py-0.5 text-xs font-medium text-amber-900 ring-1 ring-amber-200"
              title={source}
            >
              {describeDegradedSource(source)}
            </li>
          ))}
        </ul>
      </div>
    </section>
  );
}

/** Turn a `meta.degraded_sources` marker (e.g. "transport:estimated") into a readable label. */
const KNOWN_DEGRADED_SOURCES: Record<string, string> = {
  "engine:stub": "Planning engine — placeholder data",
};

function describeDegradedSource(marker: string): string {
  const known = KNOWN_DEGRADED_SOURCES[marker];
  if (known) return known;
  const [area, flag] = marker.split(":");
  const label = area ? area.charAt(0).toUpperCase() + area.slice(1) : marker;
  return flag ? `${label} — ${flag}` : label;
}

/**
 * Markers we hold real coordinates for: the city centre, the stay (if located), and every
 * itinerary activity that carries a POI coordinate. Activities without coordinates are skipped
 * rather than guessed, so the map only ever shows real, source-backed positions.
 */
function planMarkers(plan: ChosenPlan): MapMarker[] {
  const markers: MapMarker[] = [
    {
      lat: plan.destination.latitude,
      lon: plan.destination.longitude,
      label: `${plan.destination.city} centre`,
      kind: "destination",
    },
  ];
  const a = plan.accommodation;
  if (a?.latitude != null && a.longitude != null) {
    markers.push({
      lat: a.latitude,
      lon: a.longitude,
      label: a.name ?? "Accommodation",
      kind: "accommodation",
    });
  }
  for (const day of plan.itinerary) {
    for (const activity of day.activities) {
      if (activity.latitude != null && activity.longitude != null) {
        markers.push({
          lat: activity.latitude,
          lon: activity.longitude,
          label: activity.name,
          kind: "activity",
        });
      }
    }
  }
  return markers;
}

/**
 * Nights, derived from real figures: the accommodation spend divided by the nightly price. We
 * do not fall back to the itinerary length, so a mismatched or missing price yields null rather
 * than a guessed number.
 */
function estimateNights(plan: ChosenPlan): number | null {
  const a = plan.accommodation;
  if (!a || a.price_per_night <= 0) return null;
  const nights = Math.round(
    plan.cost_breakdown.accommodation / a.price_per_night,
  );
  return nights > 0 ? nights : null;
}

function formatDuration(minutes: number): string {
  const h = Math.floor(minutes / 60);
  const m = Math.round(minutes % 60);
  if (h === 0) return `${m} min`;
  return m === 0 ? `${h} h` : `${h} h ${m} min`;
}

function formatWeekday(iso: string): string {
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return iso;
  return d.toLocaleDateString("en-GB", { weekday: "short" });
}

function formatMonthDay(iso: string): string {
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return "";
  return d.toLocaleDateString("en-US", { month: "short", day: "numeric" });
}
