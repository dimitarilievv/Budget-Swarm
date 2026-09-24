/**
 * theme — shared design tokens for the UI.
 *
 * Tailwind carries the visual system, so this file holds the few tokens worth naming once:
 * the brand palette (for the rare spot that needs a colour in JS) and the shared form-field
 * class, so every input in the app looks identical without copy-pasting a long class string.
 */

/** Brand palette — teal primary with a warm amber accent and a cool supporting tone. */
export const BRAND = {
  primary: "#0d9488", // teal-600
  primarySoft: "#ccfbf1", // teal-100
  accent: "#f59e0b", // amber-500
  support: "#0ea5e9", // sky-500
  ink: "#0f172a", // slate-900
  muted: "#64748b", // slate-500
} as const;

/** The standard text/number input style, shared by every form field. */
export const INPUT_CLASS =
  "mt-1.5 w-full rounded-lg border border-slate-200 bg-white px-3.5 py-2.5 text-[15px] font-medium text-slate-900 shadow-sm focus:border-teal-500 focus:outline-none focus:ring-2 focus:ring-teal-500/20";
