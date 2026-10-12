/**
 * AccountChip — the signed-in account affordance shared by every header.
 *
 * A pill button with an initials avatar and the user's name (name hidden on phones, avatar
 * always shown) that navigates to `/profile`. One component so the account control looks and
 * behaves the same across the landing, saved-trips and shell navbars, and changes once.
 *
 * Inline-styled so it drops into both the Tailwind and inline-styled headers unchanged.
 */
import { initials } from "@/lib/initials";
import { navigate } from "@/router";

const TEAL = "#0f9488";

export default function AccountChip({
  name,
  email,
  color = "#12201e",
}: {
  name?: string | null;
  email?: string | null;
  /** Text colour for the name label, to match the surrounding header. */
  color?: string;
}) {
  const display = (name && name.trim()) || email || "Account";
  return (
    <button
      type="button"
      onClick={() => navigate("/profile")}
      title="Your account"
      aria-label="Your account"
      className="focus-visible:ring-2 focus-visible:ring-teal-500/40"
      style={{
        all: "unset",
        display: "flex",
        alignItems: "center",
        gap: 8,
        cursor: "pointer",
        padding: "4px 12px 4px 4px",
        borderRadius: 99,
        border: "1px solid #d7e2df",
        background: "#fff",
        whiteSpace: "nowrap",
      }}
    >
      <span
        aria-hidden="true"
        style={{
          display: "flex",
          alignItems: "center",
          justifyContent: "center",
          width: 26,
          height: 26,
          borderRadius: 99,
          background: TEAL,
          color: "#fff",
          fontSize: 11,
          fontWeight: 700,
        }}
      >
        {initials(name, email)}
      </span>
      <span
        className="hidden sm:inline"
        style={{ fontSize: 13, fontWeight: 600, color }}
      >
        {display}
      </span>
    </button>
  );
}
