import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it } from "vitest";

import type { Assessment, Finding } from "../types";
import FindingsList from "./FindingsList";

function finding(id: number, severity: Finding["severity"], title: string): Finding {
  return {
    id,
    test_case: 1,
    title,
    description: `Description of ${id}`,
    severity,
    category: "validation_gap",
    confidence: "high",
    probable_cause: "",
    reproduction: [],
    status: "open",
    evidence: {
      expected_status_code: 400,
      request: { method: "POST", url: "http://api.test/users", body: { age: 17 } },
      response: { status_code: 201, response_time_ms: 12, body: { id: 1 } },
    },
    created_at: "2026-09-28T10:00:00Z",
  };
}

const findings = [
  finding(1, "medium", "Accepted an age below the minimum"),
  finding(2, "high", "Crashed on a string age"),
];

const assessment: Assessment = {
  id: 1,
  finding: 2,
  verdict: "likely_real",
  confidence: "high",
  suggested_severity: "critical",
  reasoning: "The API answered 500.",
  likely_cause: "int() on user input.",
  recommendation: "Validate the type.",
};

describe("FindingsList", () => {
  it("lists the most severe first and filters by severity", async () => {
    render(<FindingsList findings={findings} assessments={new Map()} />);

    const titles = screen.getAllByText(/Accepted|Crashed/).map((el) => el.textContent);
    expect(titles).toEqual(["Crashed on a string age", "Accepted an age below the minimum"]);

    await userEvent.click(screen.getByRole("button", { name: "medium 1" }));
    expect(screen.queryByText("Crashed on a string age")).not.toBeInTheDocument();
  });

  it("shows the evidence and the AI assessment when opened", async () => {
    render(<FindingsList findings={findings} assessments={new Map([[2, assessment]])} />);

    expect(screen.getByText("Likely real")).toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: /Crashed on a string age/ }));

    expect(screen.getByText(/expected 400/)).toBeInTheDocument();
    expect(screen.getByText("The API answered 500.")).toBeInTheDocument();
    expect(screen.getByText(/suggests critical severity/)).toBeInTheDocument();
  });

  it("says so when there is nothing to show", () => {
    render(<FindingsList findings={[]} assessments={new Map()} />);

    expect(screen.getByText(/No findings/)).toBeInTheDocument();
  });
});

describe("FindingsList finding details", () => {
  it("shows the confidence, the probable cause and every step to reproduce", async () => {
    const crash: Finding = {
      ...finding(3, "high", "POST /units: duplicate create returns 500 (unhandled unique constraint)"),
      category: "crash",
      probable_cause: "The unique-constraint error is not turned into a 409.",
      reproduction: [
        { step: 1, note: "Setup: create the resource.", method: "POST", url: "http://api.test/units", body: { name: "MT-1a2b3c" }, status_code: 201 },
        { step: 2, note: "Send the same request again.", method: "POST", url: "http://api.test/units", body: { name: "MT-1a2b3c" }, status_code: 500 },
      ],
    };
    render(<FindingsList findings={[crash]} assessments={new Map()} />);

    expect(screen.getByText(/crash - high confidence/)).toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: /duplicate create/ }));

    expect(screen.getByText(/not turned into a 409/)).toBeInTheDocument();
    expect(screen.getByText("Steps to reproduce")).toBeInTheDocument();
    expect(screen.getByText(/2\. POST http:\/\/api\.test\/units -> 500/)).toBeInTheDocument();
  });
});
