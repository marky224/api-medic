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
