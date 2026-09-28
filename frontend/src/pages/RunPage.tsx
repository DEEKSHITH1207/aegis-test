import { useMemo, useState } from "react";
import { Link, useNavigate, useParams } from "react-router-dom";

import { api } from "../api";
import AnalysisPanel from "../components/AnalysisPanel";
import FindingsList from "../components/FindingsList";
import { ErrorAlert, Loading, Spinner, StatusBadge, Tile, formatDate, scopeText } from "../components/ui";
import { describe, useLoad } from "../hooks";
import type { Assessment } from "../types";

const POLL_MS = 2000;

export default function RunPage() {
  const id = Number(useParams().id);
  const navigate = useNavigate();
  const [rerunning, setRerunning] = useState(false);
  const [rerunError, setRerunError] = useState<string | null>(null);
  const run = useLoad(() => api.run(id), [id], {
    pollMs: POLL_MS,
    shouldPoll: (r) => r?.status === "running" || r?.status === "pending",
  });
  const findingCount = run.data?.results.findings ?? 0;
  // Reloaded whenever the run reports a new number of findings.
  const findings = useLoad(() => api.findings(id), [id, findingCount]);
  const features = useLoad(() => api.features(), []);
  const analyses = useLoad(() => api.analyses(id), [id], {
    pollMs: POLL_MS,
    shouldPoll: (page) => ["running", "pending"].includes(page?.results[0]?.status ?? ""),
  });
  const latest = analyses.data?.results[0];
  const latestCompleted = analyses.data?.results.find((a) => a.status === "completed");
  const assessments = useLoad(
    () => (latestCompleted ? api.assessments(latestCompleted.id) : Promise.resolve([] as Assessment[])),
    [latestCompleted?.id, latestCompleted?.status],
  );
  const byFinding = useMemo(
    () => new Map((assessments.data ?? []).map((a) => [a.finding, a])),
    [assessments.data],
  );

  if (run.error && !run.data) return <ErrorAlert message={run.error} />;
  if (!run.data) return <Loading />;
  const r = run.data;
  const finished = r.status === "completed" || r.status === "failed" || r.status === "cancelled";
  const done = r.results.total - r.results.pending - r.results.running;
  const percent = r.results.total ? Math.round((done / r.results.total) * 100) : 0;

  async function rerun() {
    setRerunning(true);
    setRerunError(null);
    try {
      const next = await api.rerun(r.id);
      navigate(`/runs/${next.id}`);
    } catch (err) {
      setRerunError(describe(err));
    } finally {
      setRerunning(false);
    }
  }

  return (
    <div className="stack">
      <div className="page-header">
        <div>
          <div className="crumbs">
            <Link to="/">Dashboard</Link> /{" "}
            <Link to={`/specifications/${r.specification}`}>{r.specification_name}</Link> / Run #{r.id}
          </div>
          <h1>{r.name}</h1>
          <p>
            <StatusBadge status={r.status} /> <code>{r.base_url}</code>
            {r.header_names.length > 0 && <span> - headers: {r.header_names.join(", ")}</span>}
          </p>
          <p>
            <strong>Scope:</strong> {scopeText(r.scope)}
          </p>
        </div>
        <div style={{ display: "flex", gap: 8, flexWrap: "wrap" }}>
          <button
            type="button"
            className="btn"
            onClick={rerun}
            disabled={!finished || rerunning}
            title="Test the same endpoints again, with the same target and headers"
          >
            {rerunning && <Spinner label="Starting" />}
            Run again
          </button>
          <a
            className="btn"
            href={api.reportUrl(r.id, true)}
            target="_blank"
            rel="noreferrer"
            aria-disabled={!finished}
          >
            View PDF
          </a>
          <a className="btn btn-primary" href={api.reportUrl(r.id)} download aria-disabled={!finished}>
            Download PDF report
          </a>
        </div>
      </div>

      {r.status === "running" && (
        <div className="card card-body">
          <div style={{ display: "flex", justifyContent: "space-between", marginBottom: 8 }}>
            <strong>Sending requests...</strong>
            <span className="muted">
              {done} of {r.results.total} cases
            </span>
          </div>
          <div className="progress" role="progressbar" aria-valuenow={percent} aria-valuemin={0} aria-valuemax={100}>
            <div style={{ width: `${percent}%` }} />
          </div>
        </div>
      )}
      <ErrorAlert message={rerunError} />
      {r.status === "failed" && r.status_detail && <div className="alert">{r.status_detail}</div>}

      <div className="tiles">
        <Tile label="Test cases" value={r.results.total} />
        <Tile label="Passed" value={r.results.passed} tone="success" />
        <Tile label="Failed" value={r.results.failed} tone={r.results.failed ? "danger" : undefined} />
        <Tile label="Errors" value={r.results.error} />
        <Tile label="Findings" value={r.results.findings} tone={r.results.findings ? "danger" : undefined} />
      </div>
      {r.results.error > 0 && finished && (
        <div className="alert warning">
          {r.results.error} case(s) got no verdict - often a 401/403 because the run needs credentials, or a
          timeout. They are listed as errors, not findings.
        </div>
      )}

      <div className="two-col">
        <section className="card">
          <div className="card-header">
            <h2>Findings</h2>
            <span className="muted">
              Started {formatDate(r.started_at)}
              {r.completed_at && ` - finished ${formatDate(r.completed_at)}`}
            </span>
          </div>
          <ErrorAlert message={findings.error} />
          {!findings.data ? <Loading /> : <FindingsList findings={findings.data} assessments={byFinding} />}
        </section>
        <div style={{ alignSelf: "start" }}>
          <AnalysisPanel
            runId={r.id}
            runFinished={finished}
            findingCount={findingCount}
            analysis={latest}
            features={features.data}
            onStarted={analyses.reload}
          />
        </div>
      </div>
    </div>
  );
}
