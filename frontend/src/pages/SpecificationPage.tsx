import { useEffect, useState, type FormEvent } from "react";
import { Link, useNavigate, useParams } from "react-router-dom";

import { api } from "../api";
import EndpointPicker from "../components/EndpointPicker";
import { ErrorAlert, Loading, Method, Spinner, StatusBadge, formatDate, scopeText } from "../components/ui";
import { describe, useLoad } from "../hooks";
import type { Endpoint } from "../types";

interface HeaderRow {
  name: string;
  value: string;
}

export default function SpecificationPage() {
  const id = Number(useParams().id);
  const spec = useLoad(() => api.specification(id), [id]);
  const endpoints = useLoad(() => api.endpoints(id), [id]);
  const runs = useLoad(() => api.runs(id), [id]);

  if (spec.error) return <ErrorAlert message={spec.error} />;
  if (!spec.data) return <Loading />;
  const s = spec.data;

  return (
    <div className="stack">
      <div className="page-header">
        <div>
          <div className="crumbs">
            <Link to="/">Dashboard</Link> / API
          </div>
          <h1>
            {s.name} {s.version && <span className="muted">{s.version}</span>}
          </h1>
          <p>
            {s.endpoint_count} endpoints - {s.source_type === "swagger" ? "Swagger 2" : "OpenAPI 3"} - uploaded{" "}
            {formatDate(s.created_at)}
          </p>
        </div>
      </div>

      <div className="two-col">
        <div className="stack">
          <section className="card">
            <div className="card-header">
              <h2>Test runs</h2>
            </div>
            <ErrorAlert message={runs.error} />
            {!runs.data ? (
              <Loading />
            ) : runs.data.results.length === 0 ? (
              <div className="empty">No runs yet. Start one with the form.</div>
            ) : (
              <div className="table-wrap">
                <table>
                  <thead>
                    <tr>
                      <th>Run</th>
                      <th>Status</th>
                      <th className="num">Findings</th>
                      <th>Report</th>
                    </tr>
                  </thead>
                  <tbody>
                    {runs.data.results.map((run) => (
                      <tr key={run.id}>
                        <td>
                          <Link to={`/runs/${run.id}`}>{run.name}</Link>
                          <div className="muted" style={{ fontSize: "0.8rem" }}>
                            {formatDate(run.created_at)} - {scopeText(run.scope)}
                          </div>
                        </td>
                        <td>
                          <StatusBadge status={run.status} />
                        </td>
                        <td className="num">{run.results.findings}</td>
                        <td>
                          {run.status === "completed" || run.status === "failed" ? (
                            <a className="btn btn-sm" href={api.reportUrl(run.id)} download>
                              PDF
                            </a>
                          ) : (
                            <span className="muted">-</span>
                          )}
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            )}
          </section>

          <section className="card">
            <div className="card-header">
              <h2>Endpoints</h2>
              <span className="muted">{endpoints.data?.length ?? ""}</span>
            </div>
            <ErrorAlert message={endpoints.error} />
            {!endpoints.data ? (
              <Loading />
            ) : endpoints.data.length === 0 ? (
              <div className="empty">This document has no importable operations.</div>
            ) : (
              <div className="table-wrap">
                <table>
                  <tbody>
                    {endpoints.data.map((endpoint) => (
                      <tr key={endpoint.id}>
                        <td style={{ width: 90 }}>
                          <Method method={endpoint.method} />
                        </td>
                        <td>
                          <code>{endpoint.path}</code>
                        </td>
                        <td className="muted">
                          {endpoint.tags.map((tag) => (
                            <span className="tag" key={tag}>
                              {tag}
                            </span>
                          ))}
                          {endpoint.summary}
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            )}
          </section>
        </div>

        <NewRunForm
          specificationId={s.id}
          suggestedBaseUrl={s.suggested_base_url}
          endpoints={endpoints.data ?? []}
          disabled={!s.endpoint_count}
        />
      </div>
    </div>
  );
}

function NewRunForm({
  specificationId,
  suggestedBaseUrl,
  endpoints,
  disabled,
}: {
  specificationId: number;
  suggestedBaseUrl: string;
  endpoints: Endpoint[];
  disabled: boolean;
}) {
  const navigate = useNavigate();
  const [scope, setScope] = useState<"all" | "some">("all");
  const [selected, setSelected] = useState<Set<number>>(new Set());
  const [name, setName] = useState("");
  const [cleanup, setCleanup] = useState(true);
  const [baseUrl, setBaseUrl] = useState(suggestedBaseUrl);
  const [headers, setHeaders] = useState<HeaderRow[]>([{ name: "Authorization", value: "" }]);
  const [step, setStep] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => setBaseUrl((current) => current || suggestedBaseUrl), [suggestedBaseUrl]);

  function updateHeader(index: number, change: Partial<HeaderRow>) {
    setHeaders((rows) => rows.map((row, i) => (i === index ? { ...row, ...change } : row)));
  }

  async function onSubmit(event: FormEvent) {
    event.preventDefault();
    setError(null);
    if (scope === "some" && selected.size === 0) {
      setError("Choose at least one section or endpoint to test.");
      return;
    }
    const sent: Record<string, string> = {};
    for (const row of headers) {
      if (row.name.trim() && row.value) sent[row.name.trim()] = row.value;
    }
    let runId: number | null = null;
    try {
      setStep("Creating the run...");
      const run = await api.createRun({
        specification: specificationId,
        name,
        base_url: baseUrl.trim(),
        headers: sent,
        endpoints: scope === "some" ? [...selected] : [],
        cleanup,
      });
      runId = run.id;
      setStep("Generating test cases...");
      await api.generate(run.id);
      setStep("Starting execution...");
      await api.execute(run.id);
      navigate(`/runs/${run.id}`);
    } catch (err) {
      setError(describe(err));
      setStep(null);
      if (runId !== null) {
        setError(`${describe(err)} The run was created as #${runId}; open it from the list to see its state.`);
      }
    }
  }

  return (
    <form className="card" onSubmit={onSubmit} style={{ alignSelf: "start" }}>
      <div className="card-header">
        <h2>New test run</h2>
      </div>
      <div className="card-body">
        <div className="field">
          <label htmlFor="base-url">Base URL of the API under test</label>
          <input
            id="base-url"
            type="url"
            required
            placeholder="https://staging.example.com/api"
            value={baseUrl}
            onChange={(e) => setBaseUrl(e.target.value)}
          />
          <span className="hint">
            {suggestedBaseUrl ? "Taken from the document; change it to test another environment." : "The document names no server; enter one."}
          </span>
        </div>

        <fieldset className="field" style={{ border: "none", padding: 0, margin: "0 0 14px" }}>
          <legend style={{ fontWeight: 600, fontSize: "0.88rem", marginBottom: 6 }}>What to test</legend>
          <label className="choice">
            <input type="radio" name="scope" checked={scope === "all"} onChange={() => setScope("all")} />
            <span>
              Everything
              <span className="hint">All {endpoints.length} endpoints</span>
            </span>
          </label>
          <label className="choice">
            <input type="radio" name="scope" checked={scope === "some"} onChange={() => setScope("some")} />
            <span>
              Choose sections or endpoints
              <span className="hint">
                {selected.size ? `${selected.size} endpoint${selected.size === 1 ? "" : "s"} selected` : "Pick by Swagger tag, or one by one"}
              </span>
            </span>
          </label>
          {scope === "some" && <EndpointPicker endpoints={endpoints} selected={selected} onChange={setSelected} />}
        </fieldset>

        <div className="field">
          <label>Headers sent with every request</label>
          {headers.map((row, index) => (
            <div className="header-row" key={index}>
              <input
                type="text"
                aria-label="Header name"
                placeholder="Header"
                value={row.name}
                onChange={(e) => updateHeader(index, { name: e.target.value })}
              />
              <input
                type="password"
                aria-label="Header value"
                placeholder="Value, e.g. Bearer ..."
                autoComplete="off"
                value={row.value}
                onChange={(e) => updateHeader(index, { value: e.target.value })}
              />
              <button
                type="button"
                className="btn btn-sm"
                aria-label="Remove header"
                onClick={() => setHeaders((rows) => rows.filter((_, i) => i !== index))}
              >
                x
              </button>
            </div>
          ))}
          <div>
            <button
              type="button"
              className="btn btn-sm"
              onClick={() => setHeaders((rows) => [...rows, { name: "", value: "" }])}
            >
              Add header
            </button>
          </div>
          <span className="hint">Credentials are stored for the run but never shown again or put in the report.</span>
        </div>

        <label className="choice" style={{ marginBottom: 14 }}>
          <input type="checkbox" checked={cleanup} onChange={(e) => setCleanup(e.target.checked)} />
          <span>
            Clean up afterwards
            <span className="hint">
              Delete the records the run creates, where the API has a DELETE for them. Anything left is listed on the run.
            </span>
          </span>
        </label>

        <div className="field">
          <label htmlFor="run-name">Run name (optional)</label>
          <input id="run-name" type="text" value={name} onChange={(e) => setName(e.target.value)} />
        </div>

        <ErrorAlert message={error} />
        <button className="btn btn-primary" type="submit" disabled={disabled || step !== null} style={{ marginTop: 8, width: "100%" }}>
          {step && <Spinner label="Working" />}
          {step ??
            (scope === "some"
              ? `Test ${selected.size} endpoint${selected.size === 1 ? "" : "s"}`
              : "Generate tests and run")}
        </button>
        {disabled && <p className="hint muted">This API has no endpoints to test.</p>}
      </div>
    </form>
  );
}
