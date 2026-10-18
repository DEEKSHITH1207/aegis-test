import type { ReactNode } from "react";

import type { RunScope, RunStatus, Severity, Verdict } from "../types";

const STATUS_LABELS: Record<RunStatus, string> = {
  pending: "Pending",
  running: "Running",
  completed: "Completed",
  failed: "Failed",
  cancelled: "Cancelled",
};

export const VERDICT_LABELS: Record<Verdict, string> = {
  likely_real: "Likely real",
  likely_false_positive: "Likely false positive",
  uncertain: "Uncertain",
};

export const SEVERITIES: Severity[] = ["critical", "high", "medium", "low", "info"];

export function StatusBadge({ status }: { status: RunStatus }) {
  return <span className={`badge ${status}`}>{STATUS_LABELS[status] ?? status}</span>;
}

export function VerdictBadge({ verdict }: { verdict: Verdict }) {
  return <span className={`badge ${verdict}`}>{VERDICT_LABELS[verdict] ?? verdict}</span>;
}

export function SeverityChip({ severity }: { severity: Severity }) {
  return <span className={`sev sev-${severity}`}>{severity}</span>;
}

export function Method({ method }: { method: string }) {
  return <span className="method">{method}</span>;
}

export function Tile({
  label,
  value,
  tone,
}: {
  label: string;
  value: ReactNode;
  tone?: "danger" | "success";
}) {
  return (
    <div className={`tile${tone ? ` is-${tone}` : ""}`}>
      <div className="tile-value">{value}</div>
      <div className="tile-label">{label}</div>
    </div>
  );
}

export function ErrorAlert({ message }: { message: string | null | undefined }) {
  if (!message) return null;
  return (
    <div className="alert" role="alert">
      {message}
    </div>
  );
}

export function Spinner({ label = "Loading" }: { label?: string }) {
  return <span className="spinner" role="status" aria-label={label} />;
}

export function Loading() {
  return (
    <div className="empty">
      <Spinner />
    </div>
  );
}

export function formatDate(value: string | null | undefined): string {
  if (!value) return "-";
  return new Date(value).toLocaleString(undefined, {
    dateStyle: "medium",
    timeStyle: "short",
  });
}

/** "All 374 endpoints", or "12 of 374 endpoints - Fleet, Citizen". */
export function scopeText(scope: RunScope | undefined): string {
  if (!scope) return "";
  if (scope.all) return `All ${scope.total} endpoints`;
  const base = `${scope.endpoints} of ${scope.total} endpoints`;
  if (!scope.tags.length) return base;
  const shown = scope.tags.slice(0, 3).join(", ");
  return `${base} - ${shown}${scope.tags.length > 3 ? ` +${scope.tags.length - 3} more` : ""}`;
}
