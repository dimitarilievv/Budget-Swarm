/**
 * Wikipedia enrichment — a best-effort fetch of a destination's summary + lead image.
 *
 * Uses the public Wikipedia REST summary endpoint (no API key, CORS-enabled). This is purely
 * descriptive enrichment for the Results page; it is never required for a plan, so every failure
 * mode (offline, 404, disambiguation, missing extract) resolves to `null` and the UI simply
 * omits the section. Content is CC BY-SA — callers must show the "From Wikipedia" attribution
 * link this returns in `pageUrl`.
 */

export interface DestinationSummary {
  title: string;
  extract: string;
  imageUrl: string | null;
  pageUrl: string;
}

const SUMMARY_ENDPOINT = "https://en.wikipedia.org/api/rest_v1/page/summary/";

/** Fetch a short summary + thumbnail for a city, or `null` when none is confidently available. */
export async function fetchDestinationSummary(
  city: string,
): Promise<DestinationSummary | null> {
  if (typeof fetch !== "function" || !city.trim()) return null;
  try {
    const res = await fetch(
      SUMMARY_ENDPOINT + encodeURIComponent(city.trim()),
      {
        headers: { accept: "application/json" },
      },
    );
    if (!res.ok) return null;
    const data: unknown = await res.json();
    if (!isSummary(data) || data.type === "disambiguation" || !data.extract) {
      return null;
    }
    return {
      title: data.title ?? city,
      extract: data.extract,
      imageUrl: data.thumbnail?.source ?? null,
      pageUrl:
        data.content_urls?.desktop?.page ??
        `https://en.wikipedia.org/wiki/${encodeURIComponent(city.trim())}`,
    };
  } catch {
    // Enrichment is optional — never surface a fetch failure to the user.
    return null;
  }
}

interface WikiSummaryShape {
  type?: string;
  title?: string;
  extract?: string;
  thumbnail?: { source?: string };
  content_urls?: { desktop?: { page?: string } };
}

function isSummary(data: unknown): data is WikiSummaryShape {
  return typeof data === "object" && data !== null;
}
