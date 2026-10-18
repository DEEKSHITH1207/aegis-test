import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";

import ConfirmTargetDialog, { classifyHost } from "./ConfirmTargetDialog";

function renderDialog(baseUrl: string) {
  const onConfirm = vi.fn();
  const onCancel = vi.fn();
  render(
    <ConfirmTargetDialog baseUrl={baseUrl} endpointCount={3} cleanup onConfirm={onConfirm} onCancel={onCancel} />,
  );
  return { onConfirm, onCancel };
}

describe("classifyHost", () => {
  it.each([
    ["localhost", "local"],
    ["127.0.0.1", "local"],
    ["host.docker.internal", "local"],
    ["192.168.1.20", "local"],
    ["172.20.0.5", "local"],
    ["api.staging.example.com", "staging"],
    ["qa-api.example.com", "staging"],
    ["dev2.example.com", "staging"],
    ["api.example.com", "other"],
    ["latest.example.com", "other"],
    ["172.40.0.5", "other"],
  ])("%s is %s", (host, kind) => {
    expect(classifyHost(host)).toBe(kind);
  });
});

describe("ConfirmTargetDialog", () => {
  it("starts a run on a staging host after one click", async () => {
    const { onConfirm } = renderDialog("https://api.staging.example.com/api");

    expect(screen.getByText("api.staging.example.com")).toBeInTheDocument();
    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: "Start run" }));

    expect(onConfirm).toHaveBeenCalledOnce();
  });

  it("makes the user type an unrecognised host before it can start", async () => {
    const { onConfirm } = renderDialog("https://api.example.com");
    const start = screen.getByRole("button", { name: "Start run" });

    expect(screen.getByRole("alert")).toHaveTextContent("Never run AegisTest against production");
    expect(start).toBeDisabled();
    await userEvent.type(screen.getByLabelText(/to confirm/), "api.example");
    expect(start).toBeDisabled();
    await userEvent.type(screen.getByLabelText(/to confirm/), ".com");
    await userEvent.click(start);

    expect(onConfirm).toHaveBeenCalledOnce();
  });

  it("cancels on Cancel and on Escape", async () => {
    const { onCancel, onConfirm } = renderDialog("http://localhost:3000");

    await userEvent.click(screen.getByRole("button", { name: "Cancel" }));
    await userEvent.keyboard("{Escape}");

    expect(onCancel).toHaveBeenCalledTimes(2);
    expect(onConfirm).not.toHaveBeenCalled();
  });
});
