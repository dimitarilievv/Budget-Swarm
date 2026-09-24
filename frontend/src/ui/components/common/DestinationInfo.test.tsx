import { render, screen, waitFor } from "@testing-library/react";
import { vi } from "vitest";

import type { DestinationSummary } from "@/api/wikipedia";
import DestinationInfo from "./DestinationInfo";

const SUMMARY: DestinationSummary = {
  title: "Budapest",
  extract: "Budapest is the capital and most populous city of Hungary.",
  imageUrl: "https://upload.wikimedia.org/budapest.jpg",
  pageUrl: "https://en.wikipedia.org/wiki/Budapest",
};

describe("DestinationInfo", () => {
  it("renders the summary, image and Wikipedia attribution", async () => {
    const load = vi.fn(async () => SUMMARY);
    render(<DestinationInfo city="Budapest" load={load} />);

    expect(
      await screen.findByText(/capital and most populous city of Hungary/i),
    ).toBeInTheDocument();
    expect(screen.getByText(/About Budapest/i)).toBeInTheDocument();

    const img = screen.getByRole("img", { name: /view of budapest/i });
    expect(img).toHaveAttribute("src", SUMMARY.imageUrl);

    const link = screen.getByRole("link", { name: /from wikipedia/i });
    expect(link).toHaveAttribute("href", SUMMARY.pageUrl);
    expect(load).toHaveBeenCalledWith("Budapest");
  });

  it("renders nothing when no summary is available", async () => {
    const load = vi.fn(async () => null);
    const { container } = render(
      <DestinationInfo city="Nowhere" load={load} />,
    );
    await waitFor(() => expect(load).toHaveBeenCalled());
    expect(container).toBeEmptyDOMElement();
  });
});
