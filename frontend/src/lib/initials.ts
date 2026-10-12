/**
 * Up to two initials from a display name, falling back to the email's first letter,
 * then "?". Shared by the nav account chips and the profile hero avatar.
 */
export function initials(name?: string | null, email?: string | null): string {
  const source = (name ?? "").trim();
  if (source) {
    return source
      .split(/\s+/)
      .slice(0, 2)
      .map((p) => p[0]!.toUpperCase())
      .join("");
  }
  const e = (email ?? "").trim();
  return (e[0] ?? "?").toUpperCase();
}
