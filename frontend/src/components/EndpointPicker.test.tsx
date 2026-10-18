import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { useState } from "react";
import { describe, expect, it } from "vitest";

import type { Endpoint } from "../types";
import EndpointPicker, { UNTAGGED, sections } from "./EndpointPicker";

const endpoint = (id: number, method: string, path: string, tags: string[]): Endpoint => ({
  id,
  method,
  path,
  tags,
  summary: "",
  operation_id: "",
});

const endpoints = [
  endpoint(1, "GET", "/trips", ["Water Transportation"]),
  endpoint(2, "POST", "/trips", ["Water Transportation"]),
  endpoint(3, "GET", "/citizens", ["Citizen"]),
  endpoint(4, "GET", "/health", []),
];

function Harness() {
  const [selected, setSelected] = useState<Set<number>>(new Set());
  return (
    <>
      <EndpointPicker endpoints={endpoints} selected={selected} onChange={setSelected} />
      <output>{[...selected].sort().join(",")}</output>
    </>
  );
}

const chosen = () => screen.getByRole("status", { hidden: true }).textContent;

describe("sections", () => {
  it("groups by tag, alphabetically, with untagged endpoints last", () => {
    expect(sections(endpoints).map((s) => s.name)).toEqual(["Citizen", "Water Transportation", UNTAGGED]);
  });

  it("lists an endpoint under each of its tags", () => {
    const both = [endpoint(9, "GET", "/x", ["A", "B"])];
    expect(sections(both).map((s) => s.endpoints.length)).toEqual([1, 1]);
  });
});

describe("EndpointPicker", () => {
  it("selects a whole section with one click", async () => {
    render(<Harness />);

    await userEvent.click(screen.getByRole("checkbox", { name: "Select section Water Transportation" }));

    expect(chosen()).toBe("1,2");
  });

  it("marks a partly selected section as indeterminate", async () => {
    render(<Harness />);

    await userEvent.click(screen.getByRole("button", { name: /Water Transportation/ }));
    await userEvent.click(screen.getByRole("checkbox", { name: /POST \/trips/ }));

    const section = screen.getByRole("checkbox", { name: "Select section Water Transportation" });
    expect((section as HTMLInputElement).indeterminate).toBe(true);
    expect(chosen()).toBe("2");
  });

  it("searches, and selects only what the search shows", async () => {
    render(<Harness />);

    await userEvent.type(screen.getByRole("textbox", { name: "Search endpoints" }), "citizen");
    expect(screen.queryByText("/trips")).not.toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: "Select shown" }));

    expect(chosen()).toBe("3");
    await userEvent.click(screen.getByRole("button", { name: "Clear" }));
    expect(chosen()).toBe("");
  });
});
