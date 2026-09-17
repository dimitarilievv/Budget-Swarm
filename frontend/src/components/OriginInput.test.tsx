import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { useState } from "react";

import { GeolocationError } from "../api/geolocation";
import OriginInput, { type OriginValue } from "./OriginInput";

const EMPTY: OriginValue = {
  originLabel: "",
  originLat: null,
  originLon: null,
};

/** A tiny controlled host so onChange updates flow back into the component. */
function Host({
  getOrigin,
  initial = EMPTY,
}: {
  getOrigin?: () => Promise<{
    originLabel: string;
    originLat: number;
    originLon: number;
  }>;
  initial?: OriginValue;
}) {
  const [value, setValue] = useState<OriginValue>(initial);
  return (
    <>
      <OriginInput value={value} onChange={setValue} getOrigin={getOrigin} />
      <output data-testid="dump">{JSON.stringify(value)}</output>
    </>
  );
}

function dump() {
  return JSON.parse(screen.getByTestId("dump").textContent ?? "{}");
}

describe("OriginInput", () => {
  it("lets the user type a starting city (no coordinates)", () => {
    render(<Host />);
    fireEvent.change(screen.getByLabelText(/starting from/i), {
      target: { value: "Belgrade" },
    });
    expect(dump()).toEqual({
      originLabel: "Belgrade",
      originLat: null,
      originLon: null,
    });
    expect(screen.getByText(/look up this city/i)).toBeInTheDocument();
  });

  it("fills coordinates + label from the browser location", async () => {
    const getOrigin = async () => ({
      originLabel: "Skopje, North Macedonia",
      originLat: 41.997,
      originLon: 21.428,
    });
    render(<Host getOrigin={getOrigin} />);
    fireEvent.click(screen.getByRole("button", { name: /use my location/i }));

    await waitFor(() =>
      expect(dump()).toEqual({
        originLabel: "Skopje, North Macedonia",
        originLat: 41.997,
        originLon: 21.428,
      }),
    );
    expect(screen.getByText(/using your location/i)).toBeInTheDocument();
  });

  it("shows a manual-entry message when permission is denied", async () => {
    const getOrigin = async () => {
      throw new GeolocationError("permission_denied");
    };
    render(<Host getOrigin={getOrigin} />);
    fireEvent.click(screen.getByRole("button", { name: /use my location/i }));

    expect(
      await screen.findByText(/location access was blocked/i),
    ).toBeInTheDocument();
    // No coordinates were set.
    expect(dump().originLat).toBeNull();
  });

  it("clears detected coordinates when the label is edited by hand", async () => {
    const getOrigin = async () => ({
      originLabel: "Skopje",
      originLat: 42,
      originLon: 21,
    });
    render(<Host getOrigin={getOrigin} />);
    fireEvent.click(screen.getByRole("button", { name: /use my location/i }));
    await waitFor(() => expect(dump().originLat).toBe(42));

    fireEvent.change(screen.getByLabelText(/starting from/i), {
      target: { value: "Vienna" },
    });
    expect(dump()).toEqual({
      originLabel: "Vienna",
      originLat: null,
      originLon: null,
    });
  });
});
