import { afterEach, describe, it, expect, vi } from "vitest";
import { fireEvent, render, screen } from "@testing-library/react";
import { HarUpload } from "./HarUpload";
import corsReport from "../../../tests/fixtures/reports/04-cors-misconfigured.json";

const FIXTURES = [
  { id: "04-cors-misconfigured", filename: "04-cors-misconfigured.json" },
];

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

function mockReportFetch() {
  vi.spyOn(globalThis, "fetch").mockImplementation(async (input) => {
    const url = typeof input === "string" ? input : (input as Request).url;
    if (url.endsWith("/fixtures/04-cors-misconfigured.json")) {
      return new Response(JSON.stringify(corsReport), { status: 200 });
    }
    return new Response("not found", { status: 404 });
  });
}

function uploadFile(file: File) {
  const input = screen.getByLabelText(/HAR file/i) as HTMLInputElement;
  fireEvent.change(input, { target: { files: [file] } });
}

describe("HarUpload", () => {
  it("Analyze button is disabled until a file is loaded", () => {
    render(<HarUpload fixtures={FIXTURES} />);
    const button = screen.getByRole("button", { name: /Analyze/ });
    expect(button).toBeDisabled();
  });

  it("rejects a non-HAR JSON file with an error message", async () => {
    render(<HarUpload fixtures={FIXTURES} />);
    uploadFile(
      new File(['{"not": "a har"}'], "junk.json", {
        type: "application/json",
      }),
    );
    expect(await screen.findByText(/not a HAR archive/i)).toBeInTheDocument();
  });

  it("accepts a valid HAR and shows the entry count", async () => {
    render(<HarUpload fixtures={FIXTURES} />);
    uploadFile(
      new File([JSON.stringify(VALID_HAR)], "session.har", {
        type: "application/json",
      }),
    );
    expect(await screen.findByText(/1 entry/)).toBeInTheDocument();
  });

  it("Analyze loads the chosen fixture and renders ReportView", async () => {
    mockReportFetch();
    render(<HarUpload fixtures={FIXTURES} />);
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
});
