// A thin client for the AegisTest API. Every call goes to /api/v1/ on the
// same origin; the Vite dev server (or whatever serves the build) forwards it
// to Django.

import type {
  Analysis,
  Assessment,
  Endpoint,
  Features,
  Finding,
  GenerationResult,
  Page,
  Specification,
  TestRun,
  UploadResult,
} from "./types";

const BASE = "/api/v1";

export class ApiError extends Error {
  constructor(
    message: string,
    readonly status: number,
    readonly body: unknown,
  ) {
    super(message);
    this.name = "ApiError";
  }
}

/**
 * One readable sentence from a DRF error body. DRF reports errors as
 * {"detail": "..."}, as {"field": ["message", ...]}, or as a list.
 */
export function errorMessage(body: unknown, fallback: string): string {
  if (typeof body === "string" && body.trim()) return body;
  if (Array.isArray(body)) {
    const parts = body.map((item) => errorMessage(item, "")).filter(Boolean);
    return parts.length ? parts.join(" ") : fallback;
  }
  if (body && typeof body === "object") {
    const parts: string[] = [];
    for (const [field, value] of Object.entries(body)) {
      const text = errorMessage(value, "");
      if (!text) continue;
      const general = field === "detail" || field === "non_field_errors" || field === "__all__";
      parts.push(general ? text : `${humanise(field)}: ${text}`);
    }
    return parts.length ? parts.join(" ") : fallback;
  }
  return fallback;
}

function humanise(field: string): string {
  const words = field.replace(/_/g, " ");
  return words.charAt(0).toUpperCase() + words.slice(1);
}

async function request<T>(path: string, init: RequestInit = {}): Promise<T> {
  const headers = new Headers(init.headers);
  headers.set("Accept", "application/json");
  if (init.body && !(init.body instanceof FormData)) {
    headers.set("Content-Type", "application/json");
  }
  let response: Response;
  try {
    response = await fetch(`${BASE}${path}`, { ...init, headers });
  } catch {
    throw new ApiError("The AegisTest server could not be reached.", 0, null);
  }
  const text = await response.text();
  let body: unknown = null;
  if (text) {
    try {
      body = JSON.parse(text);
    } catch {
      body = text;
    }
  }
  if (!response.ok) {
    const fallback =
      response.status >= 500
        ? `The server failed (${response.status}).`
        : `The request was refused (${response.status}).`;
    throw new ApiError(errorMessage(body, fallback), response.status, body);
  }
  return body as T;
}

/** Every item of a paginated list, following `next` until the end. */
export async function all<T>(path: string, limit = 5000): Promise<T[]> {
  const items: T[] = [];
  let next: string | null = path;
  while (next && items.length < limit) {
    const page: Page<T> = await request<Page<T>>(next);
    items.push(...page.results);
    next = page.next ? relative(page.next) : null;
  }
  return items;
}

// DRF's `next` is an absolute URL; keep only what follows /api/v1.
function relative(url: string): string {
  const parsed = new URL(url, window.location.origin);
  return parsed.pathname.replace(/^\/api\/v1/, "") + parsed.search;
}

const post = <T>(path: string, body?: unknown) =>
  request<T>(path, { method: "POST", body: body === undefined ? undefined : JSON.stringify(body) });

export const api = {
  features: () => request<Features>("/features/"),

  specifications: () => all<Specification>("/specifications/"),
  specification: (id: number) => request<Specification>(`/specifications/${id}/`),
  endpoints: (id: number) => all<Endpoint>(`/specifications/${id}/endpoints/`),
  upload: (file: File, name: string, version: string) => {
    const form = new FormData();
    form.append("file", file);
    form.append("name", name);
    form.append("version", version);
    return request<UploadResult>("/specifications/upload/", { method: "POST", body: form });
  },

  runs: (specificationId?: number) =>
    request<Page<TestRun>>(
      specificationId ? `/test-runs/?specification=${specificationId}` : "/test-runs/",
    ),
  run: (id: number) => request<TestRun>(`/test-runs/${id}/`),
  createRun: (body: {
    specification: number;
    name: string;
    base_url: string;
    headers: Record<string, string>;
  }) => post<TestRun>("/test-runs/", body),
  generate: (id: number) => post<GenerationResult>(`/test-runs/${id}/generate-tests/`),
  execute: (id: number) => post<TestRun>(`/test-runs/${id}/execute/`),
  findings: (id: number) => all<Finding>(`/test-runs/${id}/findings/`),
  reportUrl: (id: number, inline = false) =>
    `${BASE}/test-runs/${id}/report.pdf${inline ? "?inline=1" : ""}`,

  analyses: (runId: number) => request<Page<Analysis>>(`/analyses/?test_run=${runId}`),
  analyse: (runId: number) => post<Analysis>(`/test-runs/${runId}/analyse/`),
  assessments: (analysisId: number) => all<Assessment>(`/analyses/${analysisId}/assessments/`),
};
