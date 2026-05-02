import { afterEach, describe, it, expect, vi } from "vitest";
import { fireEvent, render, screen } from "@testing-library/react";
import { HarUpload } from "./HarUpload";
import corsReport from "../../../tests/fixtures/reports/04-cors-misconfigured.json";

const VALID_HAR = {
  log: {
    version: "1.2",
    creator: { name: "test", version: "0" },
    entries: [
      {
        request: { method: "GET", url: "https://api.example.com/v1/users" },
        response: { status: 200 },
      },
    ],
  },
};

afterEach(() => {
  vi.restoreAllMocks();
});

function mockAnalyzeFetch(report: unknown = corsReport) {
  vi.spyOn(globalThis, "fetch").mockImplementation(async (input, init) => {
    const url = typeof input === "string" ? input : (input as Request).url;
    expect(url.endsWith("/api/analyze")).toBe(true);
    expect(init?.method).toBe("POST");
    return new Response(JSON.stringify(report), { status: 200 });
  });
}

function uploadFile(file: File) {
  const input = screen.getByLabelText(/HAR file/i) as HTMLInputElement;
  fireEvent.change(input, { target: { files: [file] } });
}

describe("HarUpload", () => {
  it("Analyze button is disabled until a file is loaded", () => {
    render(<HarUpload />);
    const button = screen.getByRole("button", { name: /Analyze/ });
    expect(button).toBeDisabled();
  });

  it("rejects a non-HAR JSON file with an error message", async () => {
    render(<HarUpload />);
    uploadFile(
      new File(['{"not": "a har"}'], "junk.json", {
        type: "application/json",
      }),
    );
    expect(await screen.findByText(/not a HAR archive/i)).toBeInTheDocument();
  });

  it("accepts a valid HAR and shows the entry count", async () => {
    render(<HarUpload />);
    uploadFile(
      new File([JSON.stringify(VALID_HAR)], "session.har", {
        type: "application/json",
      }),
    );
    expect(await screen.findByText(/1 entry/)).toBeInTheDocument();
  });

  it("Analyze posts the parsed HAR to /api/analyze and renders the Report", async () => {
    mockAnalyzeFetch();
    render(<HarUpload />);
    uploadFile(
      new File([JSON.stringify(VALID_HAR)], "session.har", {
        type: "application/json",
      }),
    );
    await screen.findByText(/1 entry/);

    fireEvent.click(screen.getByRole("button", { name: /Analyze/ }));
    expect(
      await screen.findByText(/CORS preflight does not allow this origin/),
    ).toBeInTheDocument();
  });

  it("Re-run on the rendered Report re-fires /api/analyze with the same file", async () => {
    let calls = 0;
    vi.spyOn(globalThis, "fetch").mockImplementation(async (input) => {
      const url = typeof input === "string" ? input : (input as Request).url;
      expect(url.endsWith("/api/analyze")).toBe(true);
      calls++;
      return new Response(JSON.stringify(corsReport), { status: 200 });
    });

    render(<HarUpload />);
    uploadFile(
      new File([JSON.stringify(VALID_HAR)], "session.har", {
        type: "application/json",
      }),
    );
    await screen.findByText(/1 entry/);
    fireEvent.click(screen.getByRole("button", { name: /^Analyze$/ }));
    await screen.findByText(/CORS preflight/);
    expect(calls).toBe(1);

    fireEvent.click(screen.getByRole("button", { name: /^Re-run$/ }));
    await screen.findByText(/CORS preflight/);
    expect(calls).toBe(2);
  });

  it("shows size, capture date, and unique host count when present in the HAR", async () => {
    const har = {
      log: {
        version: "1.2",
        creator: { name: "test", version: "0" },
        pages: [
          { id: "p1", startedDateTime: "2026-04-29T15:00:00Z", title: "x" },
        ],
        entries: [
          {
            request: { method: "GET", url: "https://api.example.com/a" },
            response: { status: 200 },
          },
          {
            request: { method: "GET", url: "https://cdn.example.com/b" },
            response: { status: 200 },
          },
          {
            request: { method: "GET", url: "https://api.example.com/c" },
            response: { status: 200 },
          },
        ],
      },
    };
    render(<HarUpload />);
    uploadFile(
      new File([JSON.stringify(har)], "session.har", {
        type: "application/json",
      }),
    );
    await screen.findByText(/3 entries/);
    // Size, capture date, and unique host count appear on the meta line.
    expect(
      screen.getByText(/captured 2026-04-29T15:00:00Z/),
    ).toBeInTheDocument();
    // Two unique hosts (api.example.com, cdn.example.com).
    expect(screen.getByText(/2 hosts/)).toBeInTheDocument();
  });

  it("falls back to entries[0].startedDateTime when log.pages is absent", async () => {
    const har = {
      log: {
        version: "1.2",
        creator: { name: "test", version: "0" },
        entries: [
          {
            request: { method: "GET", url: "https://api.example.com/a" },
            response: { status: 200 },
            startedDateTime: "2026-04-29T16:30:00Z",
          },
        ],
      },
    };
    render(<HarUpload />);
    uploadFile(
      new File([JSON.stringify(har)], "session.har", {
        type: "application/json",
      }),
    );
    await screen.findByText(/1 entry/);
    expect(
      screen.getByText(/captured 2026-04-29T16:30:00Z/),
    ).toBeInTheDocument();
    expect(screen.getByText(/1 host/)).toBeInTheDocument();
  });

  it("omits the capture-date fragment when no startedDateTime is available anywhere", async () => {
    render(<HarUpload />);
    // VALID_HAR has no log.pages and no entries[0].startedDateTime.
    uploadFile(
      new File([JSON.stringify(VALID_HAR)], "session.har", {
        type: "application/json",
      }),
    );
    await screen.findByText(/1 entry/);
    expect(screen.queryByText(/captured /)).toBeNull();
    // Size and host count still render.
    expect(screen.getByText(/1 host/)).toBeInTheDocument();
  });

  it("forwards the HAR JSON in the analyze body", async () => {
    let body: { kind?: string; har?: unknown } = {};
    vi.spyOn(globalThis, "fetch").mockImplementation(async (_input, init) => {
      body = JSON.parse(String(init?.body ?? "{}"));
      return new Response(JSON.stringify(corsReport), { status: 200 });
    });

    render(<HarUpload />);
    uploadFile(
      new File([JSON.stringify(VALID_HAR)], "session.har", {
        type: "application/json",
      }),
    );
    await screen.findByText(/1 entry/);
    fireEvent.click(screen.getByRole("button", { name: /Analyze/ }));
    await screen.findByText(/CORS preflight/);

    expect(body.kind).toBe("har");
    expect(body.har).toEqual(VALID_HAR);
  });
});
