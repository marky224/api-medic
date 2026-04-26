import { afterEach, describe, it, expect, vi } from "vitest";
import { fireEvent, render, screen } from "@testing-library/react";
import { App } from "./App";

import healthyReport from "../../tests/fixtures/reports/01-healthy.json";
import jwtReport from "../../tests/fixtures/reports/02-jwt-expired.json";

afterEach(() => {
  vi.restoreAllMocks();
});

const FIXTURE_INDEX = [
  { id: "01-healthy", filename: "01-healthy.json" },
  { id: "02-jwt-expired", filename: "02-jwt-expired.json" },
];

function mockFixtureFetch() {
  vi.spyOn(globalThis, "fetch").mockImplementation(async (input) => {
    const url = typeof input === "string" ? input : (input as Request).url;
    if (url.endsWith("/fixtures/index.json")) {
      return new Response(JSON.stringify(FIXTURE_INDEX), { status: 200 });
    }
    if (url.endsWith("/fixtures/01-healthy.json")) {
      return new Response(JSON.stringify(healthyReport), { status: 200 });
    }
    if (url.endsWith("/fixtures/02-jwt-expired.json")) {
      return new Response(JSON.stringify(jwtReport), { status: 200 });
    }
    return new Response("not found", { status: 404 });
  });
}

describe("App", () => {
  it("renders the api-medic title and three tabs", async () => {
    mockFixtureFetch();
    render(<App />);
    expect(
      screen.getByRole("heading", { name: /api-medic/i }),
    ).toBeInTheDocument();
    expect(screen.getByRole("tab", { name: "Report" })).toBeInTheDocument();
    expect(screen.getByRole("tab", { name: "Run" })).toBeInTheDocument();
    expect(screen.getByRole("tab", { name: "HAR" })).toBeInTheDocument();
    // Let pending fixture fetches settle before the test ends to avoid
    // React act() warnings from out-of-band setState.
    await screen.findByText(/Bearer token has expired/);
  });

  it("loads and renders the default fixture in the Report tab", async () => {
    mockFixtureFetch();
    render(<App />);
    expect(
      await screen.findByText(/Bearer token has expired/),
    ).toBeInTheDocument();
  });

  it("switches fixtures via the dropdown", async () => {
    mockFixtureFetch();
    render(<App />);
    await screen.findByText(/Bearer token has expired/);

    const select = (await screen.findByLabelText(/Fixture:/i)) as HTMLSelectElement;
    fireEvent.change(select, { target: { value: "01-healthy" } });

    expect(
      await screen.findByText(/Connection negotiated HTTP\/2/),
    ).toBeInTheDocument();
  });

  it("switches to the Run tab and shows the composer", async () => {
    mockFixtureFetch();
    render(<App />);
    await screen.findByText(/Bearer token has expired/);
    fireEvent.click(screen.getByRole("tab", { name: "Run" }));
    expect(screen.getByLabelText("URL")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /^Run$/ })).toBeInTheDocument();
  });

  it("switches to the HAR tab and shows the upload surface", async () => {
    mockFixtureFetch();
    render(<App />);
    await screen.findByText(/Bearer token has expired/);
    fireEvent.click(screen.getByRole("tab", { name: "HAR" }));
    expect(screen.getByLabelText(/HAR file/i)).toBeInTheDocument();
  });
});
