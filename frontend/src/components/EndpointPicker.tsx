import { useEffect, useMemo, useRef, useState } from "react";

import type { Endpoint } from "../types";
import { Method } from "./ui";

export const UNTAGGED = "Other";

interface Section {
  name: string;
  endpoints: Endpoint[];
}

/** Endpoints grouped by the document's tags. One filed under two tags appears in both. */
export function sections(endpoints: Endpoint[]): Section[] {
  const byName = new Map<string, Endpoint[]>();
  for (const endpoint of endpoints) {
    const names = endpoint.tags.length ? endpoint.tags : [UNTAGGED];
    for (const name of names) {
      if (!byName.has(name)) byName.set(name, []);
      byName.get(name)!.push(endpoint);
    }
  }
  return [...byName.entries()]
    .sort(([a], [b]) => (a === UNTAGGED ? 1 : b === UNTAGGED ? -1 : a.localeCompare(b)))
    .map(([name, list]) => ({ name, endpoints: list }));
}

function matches(endpoint: Endpoint, query: string): boolean {
  const haystack = `${endpoint.method} ${endpoint.path} ${endpoint.summary} ${endpoint.tags.join(" ")}`;
  return haystack.toLowerCase().includes(query);
}

export default function EndpointPicker({
  endpoints,
  selected,
  onChange,
}: {
  endpoints: Endpoint[];
  selected: Set<number>;
  onChange: (next: Set<number>) => void;
}) {
  const [query, setQuery] = useState("");
  const [open, setOpen] = useState<Set<string>>(new Set());
  const q = query.trim().toLowerCase();

  const shown = useMemo(
    () =>
      sections(endpoints)
        .map((section) => ({
          ...section,
          endpoints: q ? section.endpoints.filter((e) => matches(e, q)) : section.endpoints,
        }))
        .filter((section) => section.endpoints.length > 0),
    [endpoints, q],
  );

  function set(ids: number[], on: boolean) {
    const next = new Set(selected);
    for (const id of ids) {
      if (on) next.add(id);
      else next.delete(id);
    }
    onChange(next);
  }

  function toggleOpen(name: string) {
    setOpen((current) => {
      const next = new Set(current);
      if (next.has(name)) next.delete(name);
      else next.add(name);
      return next;
    });
  }

  const shownIds = [...new Set(shown.flatMap((s) => s.endpoints.map((e) => e.id)))];

  return (
    <div className="picker">
      <div className="picker-tools">
        <input
          type="text"
          placeholder="Search path, method, summary or section"
          aria-label="Search endpoints"
          value={query}
          onChange={(e) => setQuery(e.target.value)}
        />
        <button type="button" className="btn btn-sm" onClick={() => set(shownIds, true)}>
          Select {q ? "shown" : "all"}
        </button>
        <button type="button" className="btn btn-sm" onClick={() => onChange(new Set())} disabled={!selected.size}>
          Clear
        </button>
      </div>
      <div className="picker-list">
        {shown.length === 0 && <div className="empty">No endpoint matches "{query}".</div>}
        {shown.map((section) => {
          const ids = section.endpoints.map((e) => e.id);
          const count = ids.filter((id) => selected.has(id)).length;
          const expanded = Boolean(q) || open.has(section.name);
          return (
            <div className="picker-section" key={section.name}>
              <div className="picker-section-head">
                <SectionCheckbox
                  label={`Select section ${section.name}`}
                  checked={count === ids.length}
                  indeterminate={count > 0 && count < ids.length}
                  onChange={(on) => set(ids, on)}
                />
                <button
                  type="button"
                  className="picker-section-name"
                  aria-expanded={expanded}
                  onClick={() => toggleOpen(section.name)}
                >
                  <span aria-hidden="true">{expanded ? "▾" : "▸"}</span> {section.name}
                </button>
                <span className="muted picker-count">
                  {count ? `${count}/` : ""}
                  {ids.length}
                </span>
              </div>
              {expanded &&
                section.endpoints.map((endpoint) => (
                  <label className="picker-endpoint" key={`${section.name}-${endpoint.id}`}>
                    <input
                      type="checkbox"
                      aria-label={`${endpoint.method} ${endpoint.path}`}
                      checked={selected.has(endpoint.id)}
                      onChange={(e) => set([endpoint.id], e.target.checked)}
                    />
                    <Method method={endpoint.method} />
                    <code>{endpoint.path}</code>
                    {endpoint.summary && <span className="muted picker-summary">{endpoint.summary}</span>}
                  </label>
                ))}
            </div>
          );
        })}
      </div>
    </div>
  );
}

function SectionCheckbox({
  label,
  checked,
  indeterminate,
  onChange,
}: {
  label: string;
  checked: boolean;
  indeterminate: boolean;
  onChange: (on: boolean) => void;
}) {
  const ref = useRef<HTMLInputElement>(null);
  useEffect(() => {
    if (ref.current) ref.current.indeterminate = indeterminate;
  }, [indeterminate]);
  return (
    <input
      ref={ref}
      type="checkbox"
      aria-label={label}
      checked={checked}
      onChange={(e) => onChange(e.target.checked)}
    />
  );
}
