import { useState } from "react";

import { api } from "../api";
import { describe } from "../hooks";
import type { Analysis, Features } from "../types";
import { ErrorAlert, SeverityChip, Spinner, StatusBadge, VERDICT_LABELS, formatDate } from "./ui";

export default function AnalysisPanel({
  runId,
  runFinished,
  findingCount,
  analysis,
  features,
  onStarted,
}: {
  runId: number;
  runFinished: boolean;
  findingCount: number;
  analysis: Analysis | undefined;
  features: Features | undefined;
  onStarted: () => void;
}) {
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const inProgress = analysis?.status === "running" || analysis?.status === "pending";

  async function start() {
    setBusy(true);
    setError(null);
    try {
      await api.analyse(runId);
      onStarted();
    } catch (err) {
      setError(describe(err));
    } finally {
      setBusy(false);
    }
  }

  const canStart = features?.llm_analysis && runFinished && findingCount > 0 && !inProgress;

  return (
    <section className="card">
      <div className="card-header">
        <h2>AI analysis</h2>
        {features?.llm_analysis && (
          <button className="btn btn-sm" type="button" disabled={!canStart || busy} onClick={start}>
            {(busy || inProgress) && <Spinner label="Analysing" />}
            {inProgress ? "Analysing..." : analysis ? "Analyse again" : "Analyse findings"}
          </button>
        )}
      </div>
      <div className="card-body stack" style={{ gap: 12 }}>
        <ErrorAlert message={error} />
        {!features?.llm_analysis ? (
          <p className="muted" style={{ margin: 0 }}>
            AI analysis is turned off on this server. An administrator can enable it with
            LLM_ANALYSIS_ENABLED and an Anthropic API key.
          </p>
        ) : !analysis ? (
          <p className="muted" style={{ margin: 0 }}>
            {!runFinished
              ? "Available once the run has finished."
              : findingCount === 0
                ? "Nothing to analyse: the run has no findings."
                : `Ask ${features.llm_model} for a second opinion on each finding: is it a real defect, what probably causes it, and what to do next. Findings themselves are never changed.`}
          </p>
        ) : (
          <>
            <div style={{ display: "flex", gap: 8, alignItems: "center", flexWrap: "wrap" }}>
              <StatusBadge status={analysis.status} />
              <span className="muted" style={{ fontSize: "0.85rem" }}>
                {analysis.model} - {formatDate(analysis.completed_at ?? analysis.created_at)}
              </span>
            </div>
            {analysis.status === "failed" && <ErrorAlert message={analysis.status_detail} />}
            {analysis.results.assessed > 0 && (
              <div style={{ display: "flex", gap: 14, flexWrap: "wrap", fontSize: "0.9rem" }}>
                {(Object.keys(VERDICT_LABELS) as (keyof typeof VERDICT_LABELS)[]).map((verdict) => (
                  <span key={verdict}>
                    <strong>{analysis.results[verdict]}</strong> {VERDICT_LABELS[verdict].toLowerCase()}
                  </span>
                ))}
              </div>
            )}
            {analysis.summary && <p style={{ margin: 0 }}>{analysis.summary}</p>}
            {analysis.groups.length > 0 && (
              <div>
                <div className="label">Likely root causes</div>
                {analysis.groups.map((group) => (
                  <div className="group" key={group.title}>
                    <div style={{ display: "flex", gap: 8, alignItems: "center" }}>
                      <SeverityChip severity={group.severity} />
                      <strong>{group.title}</strong>
                    </div>
                    <span>{group.explanation}</span>
                    <span>
                      <strong>Next:</strong> {group.recommendation}
                    </span>
                    <span className="muted" style={{ fontSize: "0.8rem" }}>
                      Findings {group.finding_ids.map((id) => `#${id}`).join(", ")}
                    </span>
                  </div>
                ))}
              </div>
            )}
            {analysis.warnings.length > 0 && (
              <details>
                <summary className="muted">{analysis.warnings.length} note(s) from the analysis</summary>
                <ul>
                  {analysis.warnings.map((warning) => (
                    <li key={warning} className="muted">
                      {warning}
                    </li>
                  ))}
                </ul>
              </details>
            )}
          </>
        )}
      </div>
    </section>
  );
}
