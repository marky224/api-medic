import { afterEach, describe, it, expect, vi } from "vitest";
import { fireEvent, render, screen } from "@testing-library/react";
import { RequestComposer } from "./RequestComposer";
import jwtReport from "../../../tests/fixtures/reports/02-jwt-expired.json";

afterEach(() => {
  vi.restoreAllMocks();
});

function mockRunFetch(report: unknown = jwtReport) {
  vi.spyOn(globalThis, "fetch").mockImplementation(async (input, init) => {
    const url = typeof input === "string" ? input : (input as Request).url;
    expect(url.endsWith("/api/run")).toBe(true);
    expect(init?.method).toBe("POST");
    return new Response(JSON.stringify(report), { status: 200 });
  });
}

describe("RequestComposer", () => {
  it("shows method, URL, headers, body, and a Run button", () => {
    render(<RequestComposer />);
    expect(screen.getByLabelText("Method")).toBeInTheDocument();
    expect(screen.getByLabelText("URL")).toBeInTheDocument();
    expect(screen.getByLabelText("Body")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /^Run$/ })).toBeInTheDocument();
  });

  it("can add and remove header rows", () => {
    render(<RequestComposer />);
    const before = screen.getAllByLabelText(/^Header \d+ name$/);
    fireEvent.click(screen.getByRole("button", { name: /Add header/ }));
    const after = screen.getAllByLabelText(/^Header \d+ name$/);
    expect(after.length).toBe(before.length + 1);

    fireEvent.click(screen.getByLabelText(`Remove header ${after.length}`));
    expect(screen.getAllByLabelText(/^Header \d+ name$/).length).toBe(
      before.length,
    );
  });

  it("Run posts the form to /api/run and renders the returned Report", async () => {
    mockRunFetch();
    render(<RequestComposer />);
    fireEvent.click(screen.getByRole("button", { name: /^Run$/ }));
    expect(
      await screen.findByText(/Bearer token has expired/),
    ).toBeInTheDocument();
  });

  it("forwards method, URL, headers, and body in the request body", async () => {
    let captured: { method?: string; url?: string; headers?: Record<string, string>; body?: string | null } = {};
    vi.spyOn(globalThis, "fetch").mockImplementation(async (_input, init) => {
      captured = JSON.parse(String(init?.body ?? "{}"));
      return new Response(JSON.stringify(jwtReport), { status: 200 });
    });

    render(<RequestComposer />);
    fireEvent.click(screen.getByRole("button", { name: /^Run$/ }));
    await screen.findByText(/Bearer token has expired/);

    expect(captured.method).toBe("POST");
    expect(captured.url).toBe("https://api.example.com/v1/users");
    expect(captured.headers).toEqual({
      Authorization: "Bearer ...",
      "Content-Type": "application/json",
    });
  });

  it("Re-run on the rendered Report re-fires /api/run", async () => {
    const fetchMock = vi
      .spyOn(globalThis, "fetch")
      .mockImplementation(async (input, init) => {
        const url = typeof input === "string" ? input : (input as Request).url;
        expect(url.endsWith("/api/run")).toBe(true);
        expect(init?.method).toBe("POST");
        return new Response(JSON.stringify(jwtReport), { status: 200 });
      });

    render(<RequestComposer />);
    fireEvent.click(screen.getByRole("button", { name: /^Run$/ }));
    await screen.findByText(/Bearer token has expired/);
    expect(fetchMock).toHaveBeenCalledTimes(1);

    fireEvent.click(screen.getByRole("button", { name: /^Re-run$/ }));
    await screen.findByText(/Bearer token has expired/);
    expect(fetchMock).toHaveBeenCalledTimes(2);
  });

  it("shows an error when the API responds non-2xx", async () => {
    vi.spyOn(globalThis, "fetch").mockImplementation(async () => {
      return new Response(JSON.stringify({ detail: "boom" }), { status: 500 });
    });
    render(<RequestComposer />);
    fireEvent.click(screen.getByRole("button", { name: /^Run$/ }));
    expect(await screen.findByText(/Run failed.*boom/)).toBeInTheDocument();
  });
});
