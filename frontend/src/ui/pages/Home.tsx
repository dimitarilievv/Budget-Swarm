/**
 * Home — the landing / planner page (BS-084).
 *
 * A clean, minimal split hero: the pitch on the left and the "Plan your trip" form card on
 * the right, over a soft teal/amber wash, followed by a short factual "How it works". A
 * successful submit stores the trip in {@link useTripResult}; the shell then shows Results.
 */
import TripInputForm from "@/ui/components/forms/TripInputForm";
import { IconMap, IconSearch, IconWallet } from "@/ui/components/common/Icons";
import { useTripResult } from "@/hooks/useTripResult";

const STEPS = [
  {
    Icon: IconWallet,
    title: "Set your budget",
    body: "Tell us your total budget, how long you're travelling, and what you enjoy.",
    tint: "bg-teal-50 text-teal-600",
    ring: "ring-teal-100",
  },
  {
    Icon: IconSearch,
    title: "Agents search in parallel",
    body: "Specialised AI agents compare transport, stays, weather and city highlights.",
    tint: "bg-amber-50 text-amber-600",
    ring: "ring-amber-100",
  },
  {
    Icon: IconMap,
    title: "Get one optimised trip",
    body: "A single best destination and a day-by-day itinerary that respects your wallet.",
    tint: "bg-sky-50 text-sky-600",
    ring: "ring-sky-100",
  },
];

export default function Home() {
  const { setResult } = useTripResult();

  return (
    <>
      {/* Hero: pitch left, planner card right — over a soft teal/amber wash so the
          page reads warm and alive rather than stark white. */}
      <section className="relative overflow-hidden">
        <div
          aria-hidden
          className="pointer-events-none absolute inset-0 -z-10"
          style={{
            background:
              "radial-gradient(circle at 12% 5%, rgba(251,191,36,0.19), transparent 24rem), radial-gradient(circle at 88% 22%, rgba(45,212,191,0.20), transparent 28rem), linear-gradient(180deg,#e5f5f2 0%,#f8fcfb 55%,#eef7f5 100%)",
          }}
        />

        <div className="mx-auto grid max-w-6xl gap-12 px-6 py-16 lg:grid-cols-2 lg:items-start lg:gap-16 lg:py-24">
          <div className="animate-enter">
            <span className="inline-flex items-center rounded-full bg-teal-100/80 px-3 py-1 text-xs font-semibold uppercase tracking-[0.14em] text-teal-700 ring-1 ring-teal-200/60">
              Smart itineraries, better budgets
            </span>
            <h1 className="mt-5 text-balance text-4xl font-extrabold leading-[1.05] tracking-tight text-slate-900 sm:text-5xl">
              Tell us your budget.
              <br />
              We'll find <span className="text-teal-600">where to go.</span>
            </h1>
            <p className="mt-5 max-w-md text-lg leading-relaxed text-slate-600">
              BudgetSwarm uses AI to analyse travel costs, transport logistics
              and city highlights — then builds the perfect trip that respects
              your wallet.
            </p>
            <dl className="mt-10 flex gap-8">
              <div className="flex items-center gap-2.5">
                <span className="h-2.5 w-2.5 rounded-full bg-teal-500" />
                <div>
                  <dt className="text-sm text-slate-500">Data-driven</dt>
                  <dd className="text-base font-semibold text-slate-800">
                    Real costs & routes
                  </dd>
                </div>
              </div>
              <div className="flex items-center gap-2.5">
                <span className="h-2.5 w-2.5 rounded-full bg-amber-500" />
                <div>
                  <dt className="text-sm text-slate-500">Optimised</dt>
                  <dd className="text-base font-semibold text-slate-800">
                    One plan, within budget
                  </dd>
                </div>
              </div>
            </dl>
          </div>

          <div
            style={{ "--enter-delay": "120ms" } as React.CSSProperties}
            className="animate-enter rounded-2xl border border-slate-200 bg-white shadow-[0_16px_50px_-12px_rgb(13,148,136,0.18)] ring-1 ring-teal-100/60"
          >
            <div className="h-1.5 rounded-t-2xl bg-gradient-to-r from-teal-500 via-teal-400 to-amber-400" />
            <div className="p-6 sm:p-8">
              <h2 className="text-xl font-bold tracking-tight text-slate-900">
                Plan your trip
              </h2>
              <p className="mt-1 text-sm text-slate-500">
                Fill in your details to generate optimal swarm results.
              </p>
              <div className="mt-6">
                <TripInputForm onResult={setResult} />
              </div>
            </div>
          </div>
        </div>
      </section>

      {/* How it works — factual, describes the real pipeline. A darker mint band, set off from
          the hero as a slightly recessed panel, with solid white cards that lift off it. */}
      <section className="border-t border-teal-950/[0.07] bg-gradient-to-b from-[#dcebe6] to-[#d2e6e0] shadow-[inset_0_2px_10px_-6px_rgba(13,148,136,0.25)]">
        <div className="mx-auto max-w-6xl px-6 py-16">
          <h2 className="text-2xl font-bold tracking-tight text-slate-900">
            How it works
          </h2>
          <p className="mt-2 text-slate-500">
            Three steps from a budget to a booked-ready itinerary.
          </p>
          <ol className="mt-10 grid gap-6 sm:grid-cols-3">
            {STEPS.map((step, i) => (
              <li
                key={step.title}
                style={
                  { "--enter-delay": `${i * 90}ms` } as React.CSSProperties
                }
                className={`animate-enter rounded-2xl bg-white p-6 shadow-md ring-1 transition duration-200 hover:-translate-y-1 hover:shadow-lg ${step.ring}`}
              >
                <div className="flex items-center gap-3">
                  <span
                    className={`flex h-10 w-10 items-center justify-center rounded-xl ${step.tint}`}
                  >
                    <step.Icon className="h-5 w-5" />
                  </span>
                  <span className="text-sm font-semibold text-slate-300">
                    0{i + 1}
                  </span>
                </div>
                <h3 className="mt-4 text-base font-bold text-slate-900">
                  {step.title}
                </h3>
                <p className="mt-1.5 text-sm leading-relaxed text-slate-500">
                  {step.body}
                </p>
              </li>
            ))}
          </ol>
        </div>
      </section>
    </>
  );
}
