import { describe, it, expect, vi, beforeEach } from "vitest";
import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
import fs from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";
import type { Report } from "@frontend/lib/types";

vi.mock("./lib/api", () => ({
  analyzeHarEntry: vi.fn(),
}));

import { App } from "./App";
import { analyzeHarEntry } from "./lib/api";

const mockAnalyze = vi.mocked(analyzeHarEntry);

const here = path.dirname(fileURLToPath(import.meta.url));
const HEALTHY_REPORT = JSON.parse(
  fs.readFileSync(
    path.resolve(
      here,
      "..",
      "..",
      "tests",
      "fixtures",
      "reports",
      "01-healthy.json",
    ),
    "utf8",
  ),
) as Report;

type Listener = (req: chrome.devtools.network.Request) => void;

let listeners: Listener[] = [];
const addListener = vi.fn((fn: Listener) => {
  listeners.push(fn);
});
const removeListener = vi.fn((fn: Listener) => {
  listeners = listeners.filter((l) => l !== fn);
});

beforeEach(() => {
  listeners = [];
  addListener.mockClear();
  removeListener.mockClear();
  mockAnalyze.mockReset();
  (globalThis as unknown as { chrome: unknown }).chrome = {
    devtools: {
      network: {
        onRequestFinished: { addListener, removeListener },
      },
    },
  };
});

function fireRequest(overrides: Partial<chrome.devtools.network.Request> = {}) {
  const entry = {
    request: {
      method: "GET",
      url: `https://example.com/r/${Math.random().toString(36).slice(2, 8)}`,
      headers: [],
      cookies: [],
      queryString: [],
      headersSize: -1,
      bodySize: 0,
      httpVersion: "HTTP/1.1",
    },
    response: {
      status: 200,
      statusText: "OK",
      headers: [],
      cookies: [],
      content: { size: 0, mimeType: "text/plain" },
      redirectURL: "",
      headersSize: -1,
      bodySize: 0,
      httpVersion: "HTTP/1.1",
    },
    startedDateTime: "2026-04-29T12:00:00Z",
    time: 100,
    timings: { send: 0, wait: 100, receive: 0 },
    cache: {},
    getContent: () => undefined,
    ...overrides,
  } as unknown as chrome.devtools.network.Request;
  act(() => {
    for (const l of listeners) l(entry);
  });
  return entry;
}

describe("<App />", () => {
  it("registers a request listener on mount and removes it on unmount", () => {
    const { unmount } = render(<App />);
    expect(addListener).toHaveBeenCalledTimes(1);
    expect(removeListener).not.toHaveBeenCalled();

    unmount();
    expect(removeListener).toHaveBeenCalledTimes(1);
    // Same function reference removed as was added.
    expect(removeListener.mock.calls[0]?.[0]).toBe(addListener.mock.calls[0]?.[0]);
  });

  it("shows the empty-state hint until a request is captured", () => {
    render(<App />);
    expect(
      screen.getByText(/Make a request in this tab to capture it here/i),
    ).toBeInTheDocument();
  });

  it("renders captured requests in the list", () => {
    render(<App />);
    fireRequest({
      request: {
        method: "POST",
        url: "https://api.example.com/v1/widgets",
      } as unknown as chrome.devtools.network.Request["request"],
    });
    fireRequest({
      request: {
        method: "GET",
        url: "https://api.example.com/v1/health",
      } as unknown as chrome.devtools.network.Request["request"],
    });

    expect(
      screen.getByText("https://api.example.com/v1/widgets"),
    ).toBeInTheDocument();
    expect(
      screen.getByText("https://api.example.com/v1/health"),
    ).toBeInTheDocument();
  });

  it("analyzes the selected entry and renders the returned Report", async () => {
    mockAnalyze.mockResolvedValue(HEALTHY_REPORT);
    render(<App />);
    const entry = fireRequest();

    fireEvent.click(screen.getByText(entry.request.url));
    fireEvent.click(
      screen.getByRole("button", { name: /Analyze with api-medic/i }),
    );

    await waitFor(() => {
      expect(mockAnalyze).toHaveBeenCalledWith(entry);
    });
    // ReportView renders the request URL from the report.
    expect(
      await screen.findByText(/https:\/\/api\.example\.com\/v1\/health/),
    ).toBeInTheDocument();
  });

  it("shows an error banner and no report when analyze fails", async () => {
    mockAnalyze.mockRejectedValue(new Error("Analyze failed: URL is invalid"));
    render(<App />);
    const entry = fireRequest();

    fireEvent.click(screen.getByText(entry.request.url));
    fireEvent.click(
      screen.getByRole("button", { name: /Analyze with api-medic/i }),
    );

    expect(
      await screen.findByText(/Analyze failed: URL is invalid/),
    ).toBeInTheDocument();
    // No report rendered.
    expect(screen.queryByText(/Timing breakdown/i)).not.toBeInTheDocument();
  });

  it("disables the Analyze button while a request is in flight", async () => {
    let resolveAnalyze!: (r: Report) => void;
    mockAnalyze.mockImplementation(
      () =>
        new Promise<Report>((resolve) => {
          resolveAnalyze = resolve;
        }),
    );
    render(<App />);
    const entry = fireRequest();

    fireEvent.click(screen.getByText(entry.request.url));
    const button = screen.getByRole("button", { name: /Analyze with api-medic/i });
    fireEvent.click(button);

    expect(
      await screen.findByRole("button", { name: /Analyzing/i }),
    ).toBeDisabled();

    act(() => resolveAnalyze(HEALTHY_REPORT));
    await waitFor(() => {
      expect(
        screen.getByRole("button", { name: /Analyze with api-medic/i }),
      ).not.toBeDisabled();
    });
  });

  it("Clear resets captured list, selection, report, and error", async () => {
    mockAnalyze.mockResolvedValue(HEALTHY_REPORT);
    render(<App />);
    const entry = fireRequest();
    fireEvent.click(screen.getByText(entry.request.url));
    fireEvent.click(
      screen.getByRole("button", { name: /Analyze with api-medic/i }),
    );
    await screen.findByText(/https:\/\/api\.example\.com\/v1\/health/);

    fireEvent.click(screen.getByRole("button", { name: /^Clear$/i }));

    expect(
      screen.getByText(/Make a request in this tab to capture it here/i),
    ).toBeInTheDocument();
    // Report and selection line gone.
    expect(screen.queryByText(/Timing breakdown/i)).not.toBeInTheDocument();
    expect(screen.queryByText(/^Selected:/)).not.toBeInTheDocument();
  });

  it("caps the captured list at 100 entries", () => {
    const { container } = render(<App />);
    for (let i = 0; i < 105; i++) {
      fireRequest({
        request: {
          method: "GET",
          url: `https://example.com/n/${i}`,
        } as unknown as chrome.devtools.network.Request["request"],
      });
    }
    const items = container.querySelectorAll("ul > li");
    expect(items.length).toBe(100);
    // Oldest five should have been dropped; #5 is now the head.
    expect(screen.queryByText("https://example.com/n/0")).toBeNull();
    expect(screen.queryByText("https://example.com/n/4")).toBeNull();
    expect(screen.getByText("https://example.com/n/5")).toBeInTheDocument();
    expect(screen.getByText("https://example.com/n/104")).toBeInTheDocument();
  });
});
