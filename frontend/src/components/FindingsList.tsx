import { useMemo, useState } from "react";

import type { Assessment, Finding, Severity } from "../types";
import { SEVERITIES, SeverityChip, VerdictBadge } from "./ui";

type Filter = Severity | "all";

export default function FindingsList({
  findings,
  assessments,
}: {
  findings: Finding[];
  assessments: Map<number, Assessment>;
}) {
  const [filter, setFilter] = useState<Filter>("all");
  const [open, setOpen] = useState<Set<number>>(new Set());

  const counts = useMemo(() => {
    const result = new Map<Severity, number>();
    for (const finding of findings) result.set(finding.severity, (result.get(finding.severity) ?? 0) + 1);
    return result;
  }, [findings]);

  const shown = useMemo(() => {
    const rank = (s: Severity) => SEVERITIES.indexOf(s);
    return findings
      .filter((f) => filter === "all" || f.severity === filter)
      .sort((a, b) => rank(a.severity) - rank(b.severity) || a.id - b.id);
  }, [findings, filter]);

  function toggle(id: number) {
    setOpen((current) => {
      const next = new Set(current);
      if (next.has(id)) next.delete(id);
      else next.add(id);
      return next;
    });
  }

  if (findings.length === 0) {
    return <div className="empty">No findings. Every response matched what the document promises.</div>;
  }

  return (
    <>
      <div className="card-body" style={{ paddingBottom: 12 }}>
        <div className="filters" role="group" aria-label="Filter by severity">
          <button type="button" className={`filter${filter === "all" ? " active" : ""}`} onClick={() => setFilter("all")}>
            All {findings.length}
          </button>
          {SEVERITIES.filter((s) => counts.get(s)).map((severity) => (
            <button
              key={severity}
              type="button"
              className={`filter${filter === severity ? " active" : ""}`}
              onClick={() => setFilter(severity)}
            >
              {severity} {counts.get(severity)}
            </button>
          ))}
        </div>
      </div>
      <div style={{ borderTop: "1px solid var(--border)" }}>
        {shown.map((finding) => {
          const assessment = assessments.get(finding.id);
          const expanded = open.has(finding.id);
          const request = finding.evidence.request;
          const response = finding.evidence.response;
          return (
            <div className="finding" key={finding.id}>
              <button
                type="button"
                className="finding-summary"
                aria-expanded={expanded}
                onClick={() => toggle(finding.id)}
              >
                <SeverityChip severity={finding.severity} />
                <span>
                  <span className="finding-title">{finding.title}</span>
                  <br />
                  <span className="finding-meta">
                    #{finding.id} - {finding.category.replace(/_/g, " ")}
                    {finding.confidence && ` - ${finding.confidence} confidence`}
                    {request && ` - ${request.method} ${new URL(request.url, "http://x").pathname}`}
                  </span>
                </span>
                {assessment ? <VerdictBadge verdict={assessment.verdict} /> : <span />}
              </button>
              {expanded && (
                <div className="finding-detail">
                  {finding.description && <p style={{ margin: 0 }}>{finding.description}</p>}
                  {finding.probable_cause && (
                    <p style={{ margin: 0 }}>
                      <strong>Probable cause:</strong> {finding.probable_cause}
                    </p>
                  )}
                  {finding.reproduction.length > 1 && (
                    <div className="evidence">
                      <div className="label">Steps to reproduce</div>
                      <pre>
                        {finding.reproduction
                          .map(
                            (step) =>
                              `${step.step}. ${step.method} ${step.url} -> ${step.status_code ?? "no response"}` +
                              (step.note ? `   (${step.note})` : "") +
                              (step.body !== undefined && step.body !== null ? `\n${pretty(step.body)}` : ""),
                          )
                          .join("\n\n")}
                      </pre>
                    </div>
                  )}
                  {request && finding.reproduction.length <= 1 && (
                    <div className="evidence">
                      <div className="label">Request</div>
                      <pre>
                        {request.method} {request.url}
                        {request.body !== undefined && request.body !== null
                          ? `\n\n${pretty(request.body)}`
                          : ""}
                      </pre>
                    </div>
                  )}
                  {response && (
                    <div className="evidence">
                      <div className="label">
                        Response - {response.status_code ?? "none"}
                        {finding.evidence.expected_status_code != null &&
                          ` (expected ${finding.evidence.expected_status_code})`}
                        {response.response_time_ms != null && ` - ${response.response_time_ms} ms`}
                      </div>
                      <pre>{response.body == null ? "(empty body)" : pretty(response.body)}</pre>
                    </div>
                  )}
                  {assessment && (
                    <div className="assessment">
                      <div>
                        <strong>AI assessment:</strong> <VerdictBadge verdict={assessment.verdict} />{" "}
                        <span className="muted">{assessment.confidence} confidence</span>
                        {assessment.suggested_severity !== finding.severity && (
                          <span className="muted"> - suggests {assessment.suggested_severity} severity</span>
                        )}
                      </div>
                      <p>{assessment.reasoning}</p>
                      {assessment.likely_cause && (
                        <p>
                          <strong>Likely cause:</strong> {assessment.likely_cause}
                        </p>
                      )}
                      {assessment.recommendation && (
                        <p>
                          <strong>Next step:</strong> {assessment.recommendation}
                        </p>
                      )}
                    </div>
                  )}
                </div>
              )}
            </div>
          );
        })}
      </div>
    </>
  );
}

function pretty(value: unknown): string {
  return typeof value === "string" ? value : JSON.stringify(value, null, 2);
}
