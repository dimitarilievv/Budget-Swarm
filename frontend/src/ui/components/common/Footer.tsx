/**
 * Footer — the app footer: brand line + the standard secondary links. The links are
 * placeholders until their pages exist, so they render as plain (non-navigating) items.
 */
const LINKS = ["Contact Support", "FAQ", "Terms of Service", "Privacy Policy"];

export default function Footer() {
  return (
    <footer className="border-t border-teal-950/[0.06] bg-[#f8fcfb]">
      <div className="mx-auto flex max-w-6xl flex-col gap-4 px-6 py-8 text-sm text-slate-500 sm:flex-row sm:items-center sm:justify-between">
        <div>
          <p className="font-semibold text-slate-700">
            © {new Date().getFullYear()} BudgetSwarm
          </p>
          <p className="text-xs text-slate-400">
            AI-powered travel planning for smarter budgets.
          </p>
        </div>
        <ul className="flex flex-wrap gap-x-6 gap-y-2">
          {LINKS.map((l) => (
            <li key={l}>
              <span className="cursor-default transition hover:text-slate-700">
                {l}
              </span>
            </li>
          ))}
        </ul>
      </div>
    </footer>
  );
}
