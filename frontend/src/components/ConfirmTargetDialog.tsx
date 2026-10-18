import { useEffect, useRef, useState } from "react";

export type HostKind = "local" | "staging" | "other";

// Words that name a non-production environment, matched as a whole hostname
// part ("api.staging.example.com", "qa-api.example.com", "dev2.example.com")
// so "latest.example.com" or "contest.io" do not count.
const NON_PRODUCTION = /(^|[.-])(staging|stage|stg|dev|develop|test|testing|qa|uat|sandbox|preprod)(\d*)([.-]|$)/;

/** Where a host most likely is. A guess from its name, never a guarantee. */
export function classifyHost(host: string): HostKind {
  const h = host.toLowerCase().replace(/^\[|\]$/g, "").replace(/\.$/, "");
  if (
    h === "localhost" ||
    h.endsWith(".localhost") ||
    h.endsWith(".local") ||
    h === "host.docker.internal" ||
    h === "::1" ||
    h === "0.0.0.0" ||
    /^127\./.test(h) ||
    /^10\./.test(h) ||
    /^192\.168\./.test(h) ||
    /^172\.(1[6-9]|2\d|3[01])\./.test(h)
  ) {
    return "local";
  }
  return NON_PRODUCTION.test(h) ? "staging" : "other";
}

/** The host part of a base URL, or the text itself when it does not parse. */
export function hostOf(baseUrl: string): string {
  try {
    return new URL(baseUrl).hostname;
  } catch {
    return baseUrl.trim();
  }
}

const LABELS: Record<HostKind, string> = {
  local: "Local address",
  staging: "Looks like a staging or test environment",
  other: "Not recognised as local or staging",
};

export default function ConfirmTargetDialog({
  baseUrl,
  endpointCount,
  cleanup,
  onConfirm,
  onCancel,
}: {
  baseUrl: string;
  endpointCount: number;
  cleanup: boolean;
  onConfirm: () => void;
  onCancel: () => void;
}) {
  const host = hostOf(baseUrl);
  const kind = classifyHost(host);
  const [typed, setTyped] = useState("");
  const firstFocus = useRef<HTMLInputElement & HTMLButtonElement>(null);
  const confirmed = kind !== "other" || typed.trim().toLowerCase() === host.toLowerCase();

  useEffect(() => firstFocus.current?.focus(), []);
  useEffect(() => {
    const onKey = (event: KeyboardEvent) => event.key === "Escape" && onCancel();
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [onCancel]);

  return (
    <div className="modal-backdrop" onClick={onCancel}>
      <div
        className="modal"
        role="dialog"
        aria-modal="true"
        aria-labelledby="confirm-target-title"
        onClick={(event) => event.stopPropagation()}
      >
        <h2 id="confirm-target-title">Run tests against this host?</h2>
        <p className="modal-host">
          <code>{host}</code>
          <span className={`host-kind host-kind-${kind}`}>{LABELS[kind]}</span>
        </p>
        <ul className="modal-facts">
          <li>
            Sends generated tests to {endpointCount} endpoint{endpointCount === 1 ? "" : "s"}, including invalid
            input on purpose.
          </li>
          <li>Creates test records, and edits or deletes only records the run created itself.</li>
          <li>
            {cleanup
              ? "Cleanup is on: the records it creates are deleted afterwards."
              : "Cleanup is off: the records it creates stay in the API."}
          </li>
        </ul>

        {kind === "other" && (
          <>
            <div className="alert warning" role="alert">
              This does not look like a local or staging address. Never run AegisTest against production: it
              writes test data and sends invalid requests.
            </div>
            <div className="field">
              <label htmlFor="confirm-host">
                Type <code>{host}</code> to confirm
              </label>
              <input
                id="confirm-host"
                type="text"
                autoComplete="off"
                spellCheck={false}
                ref={firstFocus}
                value={typed}
                onChange={(e) => setTyped(e.target.value)}
              />
            </div>
          </>
        )}

        <div className="modal-actions">
          <button type="button" className="btn" onClick={onCancel} ref={kind === "other" ? undefined : firstFocus}>
            Cancel
          </button>
          <button type="button" className="btn btn-primary" disabled={!confirmed} onClick={onConfirm}>
            Start run
          </button>
        </div>
      </div>
    </div>
  );
}
