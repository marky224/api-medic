import { afterEach, describe, it, expect, vi } from "vitest";
import { listFixtures, loadReport } from "./fixtures";

afterEach(() => {
  vi.restoreAllMocks();
});

function mockFetch(handler: (url: string) => Response) {
  vi.spyOn(globalThis, "fetch").mockImplementation(async (input) => {
    const url = typeof input === "string" ? input : (input as Request).url;
    return handler(url);
  });
}

describe("listFixtures", () => {
  it("fetches the manifest and returns parsed entries", async () => {
    mockFetch((url) => {
      expect(url).toBe("/fixtures/index.json");
      return new Response(
        JSON.stringify([
          { id: "01-healthy", filename: "01-healthy.json" },
          { id: "02-jwt-expired", filename: "02-jwt-expired.json" },
        ]),
        { status: 200 },
      );
    });

    const fixtures = await listFixtures();
    expect(fixtures).toHaveLength(2);
    expect(fixtures[0]).toEqual({
      id: "01-healthy",
      filename: "01-healthy.json",
    });
  });

  it("throws when the manifest request fails", async () => {
    mockFetch(() => new Response("nope", { status: 404 }));
    await expect(listFixtures()).rejects.toThrow(/404/);
  });
});

describe("loadReport", () => {
  it("fetches the fixture by id", async () => {
    mockFetch((url) => {
      expect(url).toBe("/fixtures/02-jwt-expired.json");
      return new Response(
        JSON.stringify({
          source: "har",
          request: {
            method: "POST",
            url: "https://api.example.com/v1/users",
            body_size_bytes: 0,
          },
          timing: {},
          findings: [],
        }),
        { status: 200 },
      );
    });

    const report = await loadReport("02-jwt-expired");
    expect(report.request.method).toBe("POST");
  });

  it("throws when the fixture request fails", async () => {
    mockFetch(() => new Response("", { status: 500 }));
    await expect(loadReport("missing")).rejects.toThrow(/500/);
  });
});
