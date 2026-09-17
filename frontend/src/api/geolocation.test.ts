import {
  FALLBACK_ORIGIN_LABEL,
  GeolocationError,
  getBrowserOrigin,
  reverseGeocode,
} from "./geolocation";
import { originFields } from "./types";

/** A fake Geolocation whose getCurrentPosition runs the given behaviour. */
function fakeGeolocation(
  behaviour: (success: PositionCallback, error: PositionErrorCallback) => void,
): Geolocation {
  return {
    getCurrentPosition: (
      success: PositionCallback,
      error: PositionErrorCallback,
    ) => behaviour(success, error),
    watchPosition: () => 0,
    clearWatch: () => {},
  } as unknown as Geolocation;
}

const SKOPJE = {
  coords: { latitude: 41.9973, longitude: 21.428 },
} as GeolocationPosition;

function jsonFetch(body: unknown, ok = true): typeof fetch {
  return (async () =>
    ({ ok, json: async () => body }) as Response) as unknown as typeof fetch;
}

describe("getBrowserOrigin", () => {
  it("returns coordinates and a reverse-geocoded label", async () => {
    const origin = await getBrowserOrigin({
      geolocation: fakeGeolocation((success) => success(SKOPJE)),
      fetchImpl: jsonFetch({ city: "Skopje", countryName: "North Macedonia" }),
    });
    expect(origin.originLat).toBeCloseTo(41.9973);
    expect(origin.originLon).toBeCloseTo(21.428);
    expect(origin.originLabel).toBe("Skopje, North Macedonia");
  });

  it("falls back to a generic label when reverse geocoding fails", async () => {
    const origin = await getBrowserOrigin({
      geolocation: fakeGeolocation((success) => success(SKOPJE)),
      fetchImpl: (async () => {
        throw new Error("network down");
      }) as unknown as typeof fetch,
    });
    expect(origin.originLat).toBeCloseTo(41.9973);
    expect(origin.originLabel).toBe(FALLBACK_ORIGIN_LABEL);
  });

  it("rejects with permission_denied when the user blocks location", async () => {
    await expect(
      getBrowserOrigin({
        geolocation: fakeGeolocation((_s, error) =>
          error({
            code: 1,
            message: "User denied",
          } as GeolocationPositionError),
        ),
      }),
    ).rejects.toMatchObject({ kind: "permission_denied" });
  });

  it("rejects with unsupported when geolocation is missing", async () => {
    await expect(
      // no geolocation and no navigator in this call
      getBrowserOrigin({ geolocation: undefined }),
    ).rejects.toBeInstanceOf(GeolocationError);
  });
});

describe("reverseGeocode", () => {
  it("returns null on a non-ok response", async () => {
    expect(await reverseGeocode(0, 0, jsonFetch({}, false))).toBeNull();
  });

  it("uses country alone when no city is present", async () => {
    expect(
      await reverseGeocode(0, 0, jsonFetch({ countryName: "France" })),
    ).toBe("France");
  });
});

describe("originFields", () => {
  it("maps a BrowserOrigin to the request's origin_* fields", () => {
    expect(
      originFields({ originLabel: "Skopje", originLat: 42, originLon: 21 }),
    ).toEqual({ origin_label: "Skopje", origin_lat: 42, origin_lon: 21 });
  });
});
