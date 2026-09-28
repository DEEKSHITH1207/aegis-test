import { Link, useNavigate } from "react-router-dom";

import { api } from "../api";
import { ErrorAlert, Loading, StatusBadge, Tile, formatDate } from "../components/ui";
import { useLoad } from "../hooks";

export default function DashboardPage() {
  const navigate = useNavigate();
  const specs = useLoad(() => api.specifications(), []);
  const runs = useLoad(() => api.runs(), [], {
    pollMs: 4000,
    shouldPoll: (page) => Boolean(page?.results.some((run) => run.status === "running")),
  });

  const recent = runs.data?.results ?? [];
  const findings = recent.reduce((sum, run) => sum + run.results.findings, 0);
  const running = recent.filter((run) => run.status === "running").length;

  return (
    <div className="stack">
      <div className="page-header">
        <div>
          <h1>Dashboard</h1>
          <p>Upload a Swagger or OpenAPI document, test the API it describes, download the report.</p>
        </div>
        <Link to="/upload" className="btn btn-primary">
          Upload spec
        </Link>
      </div>

      <div className="tiles">
        <Tile label="APIs" value={specs.data?.length ?? "-"} />
        <Tile label="Test runs" value={runs.data?.count ?? "-"} />
        <Tile label="Running now" value={runs.data ? running : "-"} />
        <Tile
          label="Findings in recent runs"
          value={runs.data ? findings : "-"}
          tone={findings ? "danger" : undefined}
        />
      </div>

      <section className="card">
        <div className="card-header">
          <h2>Recent test runs</h2>
        </div>
        <ErrorAlert message={runs.error} />
        {runs.loading && !runs.data ? (
          <Loading />
        ) : recent.length === 0 ? (
          <div className="empty">No test runs yet. Upload a spec to start one.</div>
        ) : (
          <div className="table-wrap">
            <table>
              <thead>
                <tr>
                  <th>Run</th>
                  <th>API</th>
                  <th>Status</th>
                  <th className="num">Passed</th>
                  <th className="num">Failed</th>
                  <th className="num">Findings</th>
                  <th>Started</th>
                  <th>Report</th>
                </tr>
              </thead>
              <tbody>
                {recent.map((run) => (
                  <tr key={run.id} className="row-link" onClick={() => navigate(`/runs/${run.id}`)}>
                    <td>
                      <Link to={`/runs/${run.id}`} onClick={(e) => e.stopPropagation()}>
                        {run.name}
                      </Link>
                    </td>
                    <td>{run.specification_name}</td>
                    <td>
                      <StatusBadge status={run.status} />
                    </td>
                    <td className="num">{run.results.passed}</td>
                    <td className="num">{run.results.failed}</td>
                    <td className="num">{run.results.findings}</td>
                    <td className="muted">{formatDate(run.started_at ?? run.created_at)}</td>
                    <td onClick={(e) => e.stopPropagation()}>
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
          <h2>APIs</h2>
          <Link to="/upload" className="btn btn-sm">
            Upload another
          </Link>
        </div>
        <ErrorAlert message={specs.error} />
        {specs.loading && !specs.data ? (
          <Loading />
        ) : !specs.data?.length ? (
          <div className="empty">
            No specifications yet. <Link to="/upload">Upload your first Swagger file.</Link>
          </div>
        ) : (
          <div className="table-wrap">
            <table>
              <thead>
                <tr>
                  <th>Name</th>
                  <th>Version</th>
                  <th>Format</th>
                  <th className="num">Endpoints</th>
                  <th>Uploaded</th>
                </tr>
              </thead>
              <tbody>
                {specs.data.map((spec) => (
                  <tr
                    key={spec.id}
                    className="row-link"
                    onClick={() => navigate(`/specifications/${spec.id}`)}
                  >
                    <td>
                      <Link to={`/specifications/${spec.id}`} onClick={(e) => e.stopPropagation()}>
                        {spec.name}
                      </Link>
                    </td>
                    <td>{spec.version || <span className="muted">-</span>}</td>
                    <td className="muted">{spec.source_type === "swagger" ? "Swagger 2" : "OpenAPI 3"}</td>
                    <td className="num">{spec.endpoint_count}</td>
                    <td className="muted">{formatDate(spec.created_at)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </section>
    </div>
  );
}
