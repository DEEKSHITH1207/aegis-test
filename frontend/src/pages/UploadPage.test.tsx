import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router-dom";
import { afterEach, describe, expect, it, vi } from "vitest";

import UploadPage from "./UploadPage";

function renderPage() {
  return render(
    <MemoryRouter>
      <UploadPage />
    </MemoryRouter>,
  );
}

const file = () => new File(['{"openapi": "3.0.0"}'], "api.json", { type: "application/json" });

describe("UploadPage", () => {
  afterEach(() => vi.restoreAllMocks());

  it("uploads the file and offers to set up a run", async () => {
    const fetch = vi.spyOn(globalThis, "fetch").mockResolvedValue(
      new Response(
        JSON.stringify({
          specification: { id: 7, name: "Directory API", version: "1.0.0" },
          import: { imported_count: 4, skipped: [], warnings: [] },
        }),
        { status: 201 },
      ),
    );
    renderPage();

    await userEvent.upload(screen.getByTestId("file-input"), file());
    await userEvent.click(screen.getByRole("button", { name: "Upload and import" }));

    expect(await screen.findByText("4 endpoints ready to test.")).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Set up a test run" })).toHaveAttribute("href", "/specifications/7");
    const [url, init] = fetch.mock.calls[0];
    expect(url).toBe("/api/v1/specifications/upload/");
    expect((init?.body as FormData).get("file")).toBeInstanceOf(File);
  });

  it("shows the server's reason when the upload is refused", async () => {
    vi.spyOn(globalThis, "fetch").mockResolvedValue(
      new Response(JSON.stringify({ name: ["A specification named 'Directory API' already exists."] }), {
        status: 400,
      }),
    );
    renderPage();

    await userEvent.upload(screen.getByTestId("file-input"), file());
    await userEvent.click(screen.getByRole("button", { name: "Upload and import" }));

    expect(await screen.findByRole("alert")).toHaveTextContent("already exists");
  });
});
