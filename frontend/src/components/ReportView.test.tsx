import { describe, it, expect, vi } from "vitest";
import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import fs from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { ReportView } from "./ReportView";
import type { Report } from "../lib/types";

const here = path.dirname(fileURLToPath(import.meta.url));
const FIXTURES_DIR = path.resolve(
  here,
  "..",
  "..",
  "..",
  "tests",
  "fixtures",
  "reports",
);

function loadFixture(filename: string): Report {
  return JSON.parse(
    fs.readFileSync(path.join(FIXTURES_DIR, filename), "utf8"),
  ) as Report;
}

const fixtureFiles = fs
  .readdirSync(FIXTURES_DIR)
  .filter((f) => f.endsWith(".json"))
  .sort();

describe("ReportView", () => {
  it.each(fixtureFiles)("renders %s without crashing", (filename) => {
    const report = loadFixture(filename);
    const { container } = render(<ReportView report={report} />);
    expect(container.firstChild).toBeTruthy();
  });

  it("renders the request method and URL", () => {
    const report = loadFixture("02-jwt-expired.json");
    render(<ReportView report={report} />);
    expect(screen.getByText(/POST/)).toBeInTheDocument();
    expect(
      screen.getByText(/https:\/\/api\.example\.com\/v1\/users/),
    ).toBeInTheDocument();
  });

  it("uses red status pill for 4xx responses", () => {
    const report = loadFixture("02-jwt-expired.json");
    const { container } = render(<ReportView report={report} />);
    const pill = container.querySelector(".bg-red-50.text-red-700");
    expect(pill).not.toBeNull();
    expect(pill?.textContent).toContain("401");
  });

  it("uses emerald status pill for 2xx responses", () => {
    const report = loadFixture("01-healthy.json");
    const { container } = render(<ReportView report={report} />);
    const pill = container.querySelector(".bg-emerald-50.text-emerald-700");
    expect(pill).not.toBeNull();
    expect(pill?.textContent).toContain("200");
  });

  it("Findings metric tier reflects the highest severity present", () => {
    const report = loadFixture("02-jwt-expired.json");
    render(<ReportView report={report} />);
    // Two critical findings in this fixture.
    expect(screen.getByText(/2 critical/)).toBeInTheDocument();
  });

  it("shows the 'No findings' empty state when findings is empty", () => {
    // 01-healthy actually contains one info-level finding; build a synthetic
    // empty-findings report to exercise the empty-state path.
    const base = loadFixture("01-healthy.json");
    const empty: Report = { ...base, findings: [] };
    render(<ReportView report={empty} />);
    expect(screen.getByText(/No findings/i)).toBeInTheDocument();
  });

  it("Findings metric shows 'N info' tier for the healthy fixture", () => {
    const report = loadFixture("01-healthy.json");
    render(<ReportView report={report} />);
    expect(screen.getByText(/1 info/)).toBeInTheDocument();
  });

  it("renders critical severity pills for jwt-expired fixture", () => {
    const report = loadFixture("02-jwt-expired.json");
    render(<ReportView report={report} />);
    const pills = screen.getAllByText("Critical");
    expect(pills.length).toBeGreaterThanOrEqual(2);
  });

  it("hides null timing rows", () => {
    // 04-cors-misconfigured has null download_ms.
    const report = loadFixture("04-cors-misconfigured.json");
    const { container } = render(<ReportView report={report} />);
    const timingSection = container.querySelector("section");
    expect(timingSection).not.toBeNull();
    // Download row should be absent because download_ms is null.
    const dnsRow = within(container as HTMLElement).queryByText("Download");
    expect(dnsRow).toBeNull();
  });

  it("renders evidence entries as key/value pairs", () => {
    const report = loadFixture("02-jwt-expired.json");
    render(<ReportView report={report} />);
    expect(screen.getByText("exp:")).toBeInTheDocument();
    expect(screen.getByText("2026-04-25T11:23:00Z")).toBeInTheDocument();
  });

  it("does not render a Share button (v1 persistence cut)", () => {
    const report = loadFixture("02-jwt-expired.json");
    render(<ReportView report={report} />);
    expect(screen.queryByRole("button", { name: /share/i })).toBeNull();
  });

  it("Re-run button invokes onRerun when supplied", () => {
    const report = loadFixture("02-jwt-expired.json");
    const onRerun = vi.fn();
    render(<ReportView report={report} onRerun={onRerun} />);
    const button = screen.getByRole("button", { name: /^Re-run$/ });
    fireEvent.click(button);
    expect(onRerun).toHaveBeenCalledTimes(1);
  });

  it("Re-run button is hidden when no onRerun handler is provided", () => {
    const report = loadFixture("02-jwt-expired.json");
    render(<ReportView report={report} />);
    expect(screen.queryByRole("button", { name: /Re-run/i })).toBeNull();
  });

  it("Re-run shows a busy label and is disabled while rerunBusy=true", () => {
    const report = loadFixture("02-jwt-expired.json");
    render(<ReportView report={report} onRerun={vi.fn()} rerunBusy />);
    const button = screen.getByRole("button", { name: /Re-running/ });
    expect(button).toBeDisabled();
  });

  it("Export markdown writes non-empty markdown to the clipboard", async () => {
    const report = loadFixture("02-jwt-expired.json");
    const writeText = vi.fn().mockResolvedValue(undefined);
    Object.assign(navigator, { clipboard: { writeText } });

    render(<ReportView report={report} />);
    fireEvent.click(screen.getByRole("button", { name: /Export markdown/ }));

    await waitFor(() => {
      expect(writeText).toHaveBeenCalledTimes(1);
    });
    const written = String(writeText.mock.calls[0]?.[0] ?? "");
    expect(written.length).toBeGreaterThan(0);
    expect(written).toMatch(/# api-medic/);
    expect(written).toMatch(/Bearer token has expired/);
    expect(await screen.findByText(/Copied to clipboard/i)).toBeInTheDocument();
  });

  it("Export markdown surfaces a failure indicator when clipboard write rejects", async () => {
    const report = loadFixture("01-healthy.json");
    const writeText = vi.fn().mockRejectedValue(new Error("nope"));
    Object.assign(navigator, { clipboard: { writeText } });

    render(<ReportView report={report} />);
    fireEvent.click(screen.getByRole("button", { name: /Export markdown/ }));
    expect(await screen.findByText(/Copy failed/i)).toBeInTheDocument();
  });
});
