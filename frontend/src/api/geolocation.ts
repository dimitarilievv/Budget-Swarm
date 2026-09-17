/**
 * Browser-geolocation origin helper.
 *
 * The trip's starting point is the user's location. `getBrowserOrigin` asks the browser
 * for the current position (latitude/longitude — exactly what the routing engine needs),
 * best-effort reverse-geocodes it to a readable "City, Country" label for display, and
 * returns both. It throws a typed `GeolocationError` when the browser has no geolocation,
 * the user denies permission, or the fix fails — so the UI can fall back to a manual
 * "starting city" input (which the backend geocodes server-side).
 *
 * Dependencies are injectable (the `Geolocation` object and `fetch`) so the helper is
 * unit-tested with no real browser API or network. Reverse geocoding uses BigDataCloud's
 * keyless client endpoint and never throws — a failure just yields the generic label.
 */

export interface BrowserOrigin {
  /** A readable "City, Country" (or a generic fallback when reverse geocoding fails). */
  originLabel: string;
  originLat: number;
  originLon: number;
}

export type GeolocationFailure =
  "unsupported" | "permission_denied" | "unavailable" | "timeout";

export class GeolocationError extends Error {
  readonly kind: GeolocationFailure;
  constructor(kind: GeolocationFailure, message?: string) {
    super(message ?? kind);
    this.name = "GeolocationError";
    this.kind = kind;
  }
}

/** Label used when the position is known but reverse geocoding could not name it. */
export const FALLBACK_ORIGIN_LABEL = "Current location";

// W3C GeolocationPositionError codes (1/2/3) — used numerically so tests need no browser.
function mapPositionError(code: number, message?: string): GeolocationError {
  if (code === 1) return new GeolocationError("permission_denied", message);
  if (code === 3) return new GeolocationError("timeout", message);
  return new GeolocationError("unavailable", message);
}

/**
 * Reverse-geocode coordinates to a "City, Country" label. Best-effort: returns `null`
 * on any failure (never throws), so the caller degrades to a generic label.
 */
export async function reverseGeocode(
  lat: number,
  lon: number,
  fetchImpl: typeof fetch = fetch,
): Promise<string | null> {
  try {
    const url =
      `https://api.bigdatacloud.net/data/reverse-geocode-client` +
      `?latitude=${lat}&longitude=${lon}&localityLanguage=en`;
    const resp = await fetchImpl(url);
    if (!resp.ok) return null;
    const data = (await resp.json()) as {
      city?: string;
      locality?: string;
      principalSubdivision?: string;
      countryName?: string;
    };
    const place =
      data.city || data.locality || data.principalSubdivision || null;
    const country = data.countryName || null;
    if (place && country) return `${place}, ${country}`;
    return place || country || null;
  } catch {
    return null;
  }
}

export interface GetBrowserOriginOptions {
  geolocation?: Geolocation;
  fetchImpl?: typeof fetch;
  positionOptions?: PositionOptions;
}

/**
 * Resolve the user's current location as a trip origin (coords + a display label).
 * Rejects with a {@link GeolocationError} when geolocation is unavailable or denied.
 */
export async function getBrowserOrigin(
  options: GetBrowserOriginOptions = {},
): Promise<BrowserOrigin> {
  const geo =
    options.geolocation ??
    (typeof navigator !== "undefined" ? navigator.geolocation : undefined);
  if (!geo) {
    throw new GeolocationError(
      "unsupported",
      "geolocation is not available in this browser",
    );
  }

  const position = await new Promise<GeolocationPosition>((resolve, reject) => {
    geo.getCurrentPosition(
      resolve,
      (err) => reject(mapPositionError(err.code, err.message)),
      options.positionOptions ?? {
        enableHighAccuracy: false,
        timeout: 10_000,
        maximumAge: 300_000,
      },
    );
  });

  const { latitude, longitude } = position.coords;
  const label =
    (await reverseGeocode(latitude, longitude, options.fetchImpl ?? fetch)) ??
    FALLBACK_ORIGIN_LABEL;
  return { originLabel: label, originLat: latitude, originLon: longitude };
}
