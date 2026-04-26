import { afterEach, describe, it, expect, vi } from "vitest";
import { fireEvent, render, screen } from "@testing-library/react";
import { RequestComposer } from "./RequestComposer";
import jwtReport from "../../../tests/fixtures/reports/02-jwt-expired.json";
import healthyReport from "../../../tests/fixtures/reports/01-healthy.json";

const FIXTURES = [
  { id: "01-healthy", filename: "01-healthy.json" },
  { id: "02-jwt-expired", filename: "02-jwt-expired.json" },
];

afterEach(() => {
  vi.restoreAllMocks();
});

function mockReportFetch() {
  vi.spyOn(globalThis, "fetch").mockImplementation(async (input) => {
    const url = typeof input === "string" ? input : (input as Request).url;
    if (url.endsWith("/fixtures/02-jwt-expired.json")) {
      return new Response(JSON.stringify(jwtReport), { status: 200 });
    }
    if (url.endsWith("/fixtures/01-healthy.json")) {
      return new Response(JSON.stringify(healthyReport), { status: 200 });
    }
    return new Response("not found", { status: 404 });
  });
}

describe("RequestComposer", () => {
  it("shows method, URL, headers, body, and a Run button", () => {
    render(<RequestComposer fixtures={FIXTURES} />);
    expect(screen.getByLabelText("Method")).toBeInTheDocument();
    expect(screen.getByLabelText("URL")).toBeInTheDocument();
    expect(screen.getByLabelText("Body")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /^Run$/ })).toBeInTheDocument();
  });

  it("can add and remove header rows", () => {
    render(<RequestComposer fixtures={FIXTURES} />);
    const before = screen.getAllByLabelText(/^Header \d+ name$/);
    fireEvent.click(screen.getByRole("button", { name: /Add header/ }));
    const after = screen.getAllByLabelText(/^Header \d+ name$/);
    expect(after.length).toBe(before.length + 1);

    fireEvent.click(
      screen.getByLabelText(`Remove header ${after.length}`),
    );
    expect(screen.getAllByLabelText(/^Header \d+ name$/).length).toBe(
      before.length,
    );
  });

  it("Run button loads the chosen fixture and renders ReportView", async () => {
    mockReportFetch();
    render(<RequestComposer fixtures={FIXTURES} />);

    fireEvent.click(screen.getByRole("button", { name: /^Run$/ }));
    expect(
      await screen.findByText(/Bearer token has expired/),
    ).toBeInTheDocument();
  });

  it("returns the fixture chosen in the picker, not always the default", async () => {
    mockReportFetch();
    render(<RequestComposer fixtures={FIXTURES} />);

    const picker = screen.getByLabelText(
      /Phase 2 — return fixture/i,
    ) as HTMLSelectElement;
    fireEvent.change(picker, { target: { value: "01-healthy" } });
    fireEvent.click(screen.getByRole("button", { name: /^Run$/ }));

    expect(
      await screen.findByText(/Connection negotiated HTTP\/2/),
    ).toBeInTheDocument();
  });
});
