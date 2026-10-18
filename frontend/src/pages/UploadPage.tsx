import { useRef, useState, type DragEvent, type FormEvent } from "react";
import { Link } from "react-router-dom";

import { api } from "../api";
import { ErrorAlert, Spinner } from "../components/ui";
import { describe } from "../hooks";
import type { UploadResult } from "../types";

const ACCEPT = ".json,.yaml,.yml,application/json,application/yaml,text/yaml";

export default function UploadPage() {
  const [file, setFile] = useState<File | null>(null);
  const [name, setName] = useState("");
  const [version, setVersion] = useState("");
  const [over, setOver] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [result, setResult] = useState<UploadResult | null>(null);
  const input = useRef<HTMLInputElement>(null);

  function choose(files: FileList | null) {
    const chosen = files?.[0];
    if (!chosen) return;
    setFile(chosen);
    setError(null);
    setResult(null);
  }

  function onDrop(event: DragEvent) {
    event.preventDefault();
    setOver(false);
    choose(event.dataTransfer.files);
  }

  async function onSubmit(event: FormEvent) {
    event.preventDefault();
    if (!file) {
      setError("Choose a Swagger or OpenAPI file first.");
      return;
    }
    setBusy(true);
    setError(null);
    try {
      setResult(await api.upload(file, name, version));
    } catch (err) {
      setError(describe(err));
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="stack" style={{ maxWidth: 760 }}>
      <div className="page-header">
        <div>
          <div className="crumbs">
            <Link to="/">Dashboard</Link> / Upload
          </div>
          <h1>Upload a Swagger / OpenAPI file</h1>
          <p>JSON or YAML, OpenAPI 3.x or Swagger 2.0. Its operations become the endpoints AegisTest tests.</p>
        </div>
      </div>

      {result ? (
        <UploadSummary result={result} onAnother={() => { setResult(null); setFile(null); }} />
      ) : (
        <form className="card card-body" onSubmit={onSubmit}>
          <div
            className={`dropzone${over ? " is-over" : ""}`}
            role="button"
            tabIndex={0}
            onClick={() => input.current?.click()}
            onKeyDown={(e) => (e.key === "Enter" || e.key === " ") && input.current?.click()}
            onDragOver={(e) => {
              e.preventDefault();
              setOver(true);
            }}
            onDragLeave={() => setOver(false)}
            onDrop={onDrop}
          >
            <strong>{file ? file.name : "Drop your spec file here"}</strong>
            <span>
              {file ? `${(file.size / 1024).toFixed(1)} KB - click to choose another` : "or click to browse (.json, .yaml, .yml)"}
            </span>
            <input
              ref={input}
              type="file"
              accept={ACCEPT}
              hidden
              data-testid="file-input"
              onChange={(e) => choose(e.target.files)}
            />
          </div>

          <div className="two-col" style={{ marginTop: 18 }}>
            <div className="field">
              <label htmlFor="name">Name</label>
              <input id="name" type="text" value={name} onChange={(e) => setName(e.target.value)} />
              <span className="hint">Leave empty to use the document's title.</span>
            </div>
            <div className="field">
              <label htmlFor="version">Version</label>
              <input id="version" type="text" value={version} onChange={(e) => setVersion(e.target.value)} />
              <span className="hint">Defaults to the document's version.</span>
            </div>
          </div>

          <ErrorAlert message={error} />
          <div style={{ marginTop: 16 }}>
            <button className="btn btn-primary" type="submit" disabled={busy || !file}>
              {busy && <Spinner label="Uploading" />}
              {busy ? "Uploading..." : "Upload and import"}
            </button>
          </div>
        </form>
      )}
    </div>
  );
}

function UploadSummary({ result, onAnother }: { result: UploadResult; onAnother: () => void }) {
  const { specification, import: imported } = result;
  return (
    <section className="card">
      <div className="card-header">
        <h2>
          Imported {specification.name}
          {specification.version && ` ${specification.version}`}
        </h2>
      </div>
      <div className="card-body stack">
        <div className="alert success">
          {imported.imported_count} endpoint{imported.imported_count === 1 ? "" : "s"} ready to test.
        </div>
        {imported.skipped.length > 0 && (
          <div className="alert warning">
            <strong>{imported.skipped.length} operation(s) skipped:</strong>
            <ul style={{ margin: "6px 0 0", paddingLeft: 18 }}>
              {imported.skipped.map((op) => (
                <li key={`${op.method} ${op.path}`}>
                  <code>{op.method} {op.path}</code> - {op.reason}
                </li>
              ))}
            </ul>
          </div>
        )}
        {imported.warnings.length > 0 && (
          <div className="alert warning">
            <ul style={{ margin: 0, paddingLeft: 18 }}>
              {imported.warnings.map((warning) => (
                <li key={warning}>{warning}</li>
              ))}
            </ul>
          </div>
        )}
        <div style={{ display: "flex", gap: 8 }}>
          <Link className="btn btn-primary" to={`/specifications/${specification.id}`}>
            Set up a test run
          </Link>
          <button className="btn" type="button" onClick={onAnother}>
            Upload another
          </button>
        </div>
      </div>
    </section>
  );
}
