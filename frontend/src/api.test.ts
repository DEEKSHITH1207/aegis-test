import { afterEach, describe, expect, it, vi } from "vitest";

import { ApiError, all, errorMessage } from "./api";

describe("errorMessage", () => {
  it("reads DRF's shapes", () => {
    expect(errorMessage({ detail: "Only a pending run can be executed." }, "x")).toBe(
      "Only a pending run can be executed.",
    );
    expect(errorMessage({ base_url: ["Enter a valid URL."] }, "x")).toBe("Base url: Enter a valid URL.");
    expect(errorMessage({ file: ["Too big."], name: ["Taken."] }, "x")).toBe("File: Too big. Name: Taken.");
    expect(errorMessage(["One.", "Two."], "x")).toBe("One. Two.");
  });

  it("falls back when there is nothing to say", () => {
    expect(errorMessage(null, "fallback")).toBe("fallback");
    expect(errorMessage({}, "fallback")).toBe("fallback");
  });
});

describe("all", () => {
  afterEach(() => vi.restoreAllMocks());

  it("follows next links until the last page", async () => {
    const pages: Record<string, unknown> = {
      "/api/v1/items/": { count: 3, next: "http://localhost:8000/api/v1/items/?page=2", results: [1, 2] },
      "/api/v1/items/?page=2": { count: 3, next: null, results: [3] },
    };
    vi.spyOn(globalThis, "fetch").mockImplementation(async (input) =>
      new Response(JSON.stringify(pages[String(input)]), { status: 200 }),
    );

    expect(await all<number>("/items/")).toEqual([1, 2, 3]);
  });

  it("raises an ApiError with the server's message", async () => {
    vi.spyOn(globalThis, "fetch").mockImplementation(
      async () => new Response(JSON.stringify({ detail: "Not found." }), { status: 404 }),
    );

    await expect(all("/items/")).rejects.toEqual(expect.objectContaining({ status: 404, message: "Not found." }));
    await expect(all("/items/")).rejects.toBeInstanceOf(ApiError);
  });
});
