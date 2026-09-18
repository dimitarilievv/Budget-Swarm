/**
 * Home / Input page (BS-084 redesign).
 * Hero background, brand header, headline, and the trip Input card. Results and
 * Comparison pages arrive in later tickets.
 */
import TripInputForm from "./components/TripInputForm";

const TRUST = [
  {
    icon: "🛡️",
    title: "Trusted by travelers",
    note: "Your data is secure and private",
  },
  {
    icon: "✨",
    title: "AI-Powered Discovery",
    note: "Multiple agents. One perfect trip.",
  },
  {
    icon: "🌐",
    title: "Real-time data",
    note: "Flights, stays, weather & more",
  },
];

export default function App() {
  return (
    <div className="relative min-h-screen bg-slate-100 text-slate-900">
      {/* Cappadocia hero photo (public/hero.jpg) with a light wash so the headline and
          card stay legible over it. */}
      <div
        className="absolute inset-0 bg-cover bg-center"
        style={{ backgroundImage: "url(/hero.jpg)" }}
        aria-hidden
      />
      <div
        className="absolute inset-0 bg-gradient-to-b from-white/80 via-white/45 to-white/85"
        aria-hidden
      />

      <div className="relative">
        {/* Header */}
        <header className="mx-auto flex max-w-6xl items-center justify-between px-6 py-5">
          <div className="flex items-center gap-2">
            <span className="text-2xl" aria-hidden>
              🌍
            </span>
            <div className="leading-tight">
              <p className="text-lg font-bold">
                <span className="text-slate-900">Budget</span>
                <span className="text-teal-600">Swarm</span>
              </p>
              <p className="text-xs text-slate-500">AI Travel Planner</p>
            </div>
          </div>
          <span className="rounded-full bg-white/80 px-4 py-1.5 text-sm font-medium text-slate-600 shadow-sm ring-1 ring-slate-200">
            ✨ AI finds. You enjoy.
          </span>
        </header>

        {/* Hero + form */}
        <main className="mx-auto max-w-3xl px-6 pb-16">
          <h1 className="mt-6 text-balance text-center text-4xl font-extrabold tracking-tight text-slate-800 sm:text-5xl">
            Tell us your budget.
            <br />
            We'll find <span className="text-teal-600">where to go.</span>
          </h1>
          <p className="mx-auto mt-4 max-w-xl text-center text-lg text-slate-600">
            Enter your trip constraints and our AI agents will discover the best
            destination and build your perfect itinerary.
          </p>

          <div className="mt-8 rounded-3xl bg-white/95 p-6 shadow-xl ring-1 ring-slate-200 backdrop-blur sm:p-8">
            <TripInputForm />
          </div>

          {/* Trust row */}
          <ul className="mt-8 grid gap-4 text-left sm:grid-cols-3">
            {TRUST.map((t) => (
              <li key={t.title} className="flex items-start gap-3">
                <span className="text-xl" aria-hidden>
                  {t.icon}
                </span>
                <div>
                  <p className="text-sm font-semibold text-slate-800">
                    {t.title}
                  </p>
                  <p className="text-xs text-slate-500">{t.note}</p>
                </div>
              </li>
            ))}
          </ul>
        </main>
      </div>
    </div>
  );
}
