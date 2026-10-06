/**
 * Navbar — the top navigation bar for the app shell.
 *
 * Left: the BudgetSwarm wordmark + swarm mark (clicking it returns to the planner).
 * Centre: primary sections (Plan Trip is active; My Trips and Compare are placeholders for
 * upcoming tickets, so they render disabled rather than as dead links). Right: a Profile
 * action. Kept intentionally minimal — a single hairline divider, no shadow.
 */
import { useOptionalAuth } from "@/contexts/AuthContext";
import { navigate } from "@/router";
import {
  IconBriefcase,
  IconCompare,
  IconSearch,
  IconUser,
} from "@/ui/components/common/Icons";

interface NavbarProps {
  /** Return to the planner / home view. */
  onHome?: () => void;
  /** Show the destination comparison; omitted (undefined) when there's nothing to compare. */
  onCompare?: () => void;
  /** Whether the comparison view is the one currently showing. */
  compareActive?: boolean;
}

export default function Navbar({
  onHome,
  onCompare,
  compareActive = false,
}: NavbarProps) {
  const auth = useOptionalAuth();
  const authed = auth?.status === "authenticated";
  const navItems = [
    {
      key: "plan",
      label: "Plan Trip",
      Icon: IconSearch,
      active: !compareActive,
      onClick: onHome,
      enabled: true,
    },
    {
      key: "trips",
      label: "My Trips",
      Icon: IconBriefcase,
      active: false,
      onClick: () => navigate("/my-trips"),
      enabled: true,
    },
    {
      key: "compare",
      label: "Compare",
      Icon: IconCompare,
      active: compareActive,
      onClick: onCompare,
      enabled: Boolean(onCompare),
    },
  ];

  return (
    <header className="sticky top-0 z-20 border-b border-teal-950/[0.07] bg-[#f5fbfa]/75 backdrop-blur-xl">
      <nav className="mx-auto flex h-16 max-w-6xl items-center justify-between px-6">
        <button
          type="button"
          onClick={onHome}
          className="flex items-center gap-2.5 rounded-lg focus:outline-none focus-visible:ring-2 focus-visible:ring-teal-500/40"
        >
          <img
            src="/logo.svg"
            alt=""
            aria-hidden="true"
            className="h-9 w-9 rounded-xl shadow-sm"
          />
          <span className="text-[17px] font-bold tracking-tight text-slate-900">
            Budget<span className="text-teal-600">Swarm</span>
          </span>
        </button>

        <ul className="hidden items-center gap-1 md:flex">
          {navItems.map((item) => (
            <li key={item.key}>
              <button
                type="button"
                disabled={!item.enabled}
                onClick={item.onClick}
                aria-current={item.active ? "page" : undefined}
                title={item.enabled ? undefined : "Coming soon"}
                className={`flex items-center gap-2 rounded-lg px-3 py-2 text-sm font-medium transition ${
                  item.active
                    ? "bg-white text-slate-900 shadow-sm ring-1 ring-slate-200"
                    : item.enabled
                      ? "text-slate-600 hover:bg-white/60 hover:text-slate-900"
                      : "cursor-not-allowed text-slate-400"
                }`}
              >
                <item.Icon className="h-4 w-4" />
                {item.label}
              </button>
            </li>
          ))}
        </ul>

        {authed ? (
          <div className="flex items-center gap-3">
            <span className="hidden items-center gap-2 text-sm font-medium text-slate-700 sm:flex">
              <IconUser className="h-4 w-4 text-teal-600" />
              {auth?.user?.name || auth?.user?.email}
            </span>
            <button
              type="button"
              onClick={() => auth?.logout()}
              className="rounded-full border border-slate-200 bg-white px-4 py-1.5 text-sm font-medium text-slate-700 transition hover:border-slate-300 hover:text-slate-900"
            >
              Log out
            </button>
          </div>
        ) : (
          <button
            type="button"
            onClick={() =>
              navigate(
                `/login?next=${encodeURIComponent(window.location.pathname + window.location.search)}`,
              )
            }
            className="flex items-center gap-2 rounded-full bg-teal-600 px-4 py-1.5 text-sm font-semibold text-white transition hover:bg-teal-700"
          >
            <IconUser className="h-4 w-4" />
            Log in
          </button>
        )}
      </nav>
    </header>
  );
}
