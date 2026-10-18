import { useState } from "react";

import { api } from "../api";
import { describe, useLoad } from "../hooks";
import type { TestRun } from "../types";
import { ErrorAlert, Loading, Spinner } from "./ui";

/** What a run created in the API under test, and a way to delete it. */
export default function ResourcesPanel({ run, finished }: { run: TestRun; finished: boolean }) {
  const counts = run.resources;
  const total = counts.deleted + counts.failed + counts.left;
  const resources = useLoad(() => api.resources(run.id), [run.id, total, counts.deleted, finished]);
  const [working, setWorking] = useState(false);
  const [error, setError] = useState<string | null>(null);

  if (total === 0) return null;
  const remaining = (resources.data ?? []).filter((r) => r.status !== "deleted");
  const deletable = remaining.some((r) => r.delete_url !== "");

  async function cleanup() {
    setWorking(true);
    setError(null);
    try {
      await api.cleanup(run.id);
      resources.reload();
    } catch (err) {
      setError(describe(err));
    } finally {
      setWorking(false);
    }
  }

  return (
    <section className="card" style={{ marginTop: 16 }}>
      <div className="card-header">
        <h2>Data created</h2>
        <span className="muted">
          {resources.data
            ? `${resources.data.length} created - ${resources.data.length - remaining.length} deleted - ${remaining.length} left`
            : `${total} created`}
        </span>
      </div>
      <div className="card-body">
        <ErrorAlert message={resources.error ?? error} />
        {!resources.data ? (
          <Loading />
        ) : remaining.length === 0 ? (
          <p className="muted" style={{ margin: 0 }}>
            Everything the run created has been deleted.
          </p>
        ) : (
          <>
            <ul className="resource-list">
              {remaining.map((resource) => (
                <li key={resource.id}>
                  <code>{resource.endpoint_label}</code> {resource.resource_id ? `#${resource.resource_id}` : "(no ID)"}
                  <br />
                  <span className="hint">{resource.detail || "Not cleaned up yet."}</span>
                </li>
              ))}
            </ul>
            {finished && deletable && (
              <button type="button" className="btn btn-sm" onClick={cleanup} disabled={working}>
                {working && <Spinner label="Deleting" />}
                Delete what can be deleted
              </button>
            )}
          </>
        )}
      </div>
    </section>
  );
}
