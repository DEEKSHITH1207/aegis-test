// Shapes returned by the AegisTest API (/api/v1/). Kept in step with
// backend/apps/testing/serializers.py by hand.

export type Severity = "critical" | "high" | "medium" | "low" | "info";
export type RunStatus = "pending" | "running" | "completed" | "failed" | "cancelled";
export type Verdict = "likely_real" | "likely_false_positive" | "uncertain";

export interface Page<T> {
  count: number;
  next: string | null;
  previous: string | null;
  results: T[];
}

export interface Specification {
  id: number;
  name: string;
  description: string;
  source_type: string;
  version: string;
  endpoint_count: number;
  suggested_base_url: string;
  created_at: string;
  updated_at: string;
}

export interface ImportResult {
  source_type: string;
  title: string;
  api_version: string;
  created: number;
  updated: number;
  unchanged: number;
  imported_count: number;
  stale: { id: number; path: string; method: string }[];
  skipped: { path: string; method: string; reason: string }[];
  warnings: string[];
}

export interface UploadResult {
  specification: Specification;
  import: ImportResult;
}

export interface Endpoint {
  id: number;
  path: string;
  method: string;
  summary: string;
  operation_id: string;
}

export interface RunResults {
  total: number;
  pending: number;
  running: number;
  passed: number;
  failed: number;
  error: number;
  skipped: number;
  findings: number;
}

export interface TestRun {
  id: number;
  specification: number;
  specification_name: string;
  name: string;
  base_url: string;
  header_names: string[];
  status: RunStatus;
  status_detail: string;
  results: RunResults;
  started_at: string | null;
  completed_at: string | null;
  created_at: string;
}

export interface GenerationResult {
  created: number;
  updated: number;
  unchanged: number;
  skipped: number;
  warnings: string[];
}

export interface Finding {
  id: number;
  test_case: number | null;
  title: string;
  description: string;
  severity: Severity;
  category: string;
  status: string;
  evidence: {
    test_type?: string;
    expected_status_code?: number | null;
    request?: { method: string; url: string; query?: object; body?: unknown };
    response?: { status_code: number | null; response_time_ms?: number; body?: unknown };
  };
  created_at: string;
}

export interface AnalysisGroup {
  title: string;
  finding_ids: number[];
  explanation: string;
  recommendation: string;
  severity: Severity;
}

export interface Analysis {
  id: number;
  test_run: number | null;
  rules_run: number | null;
  status: RunStatus;
  status_detail: string;
  model: string;
  summary: string;
  groups: AnalysisGroup[];
  warnings: string[];
  findings_total: number;
  findings_selected: number;
  results: { assessed: number } & Record<Verdict, number>;
  created_at: string;
  completed_at: string | null;
}

export interface Assessment {
  id: number;
  finding: number;
  verdict: Verdict;
  confidence: "low" | "medium" | "high";
  suggested_severity: Severity;
  reasoning: string;
  likely_cause: string;
  recommendation: string;
}

export interface Features {
  llm_analysis: boolean;
  llm_model: string;
}
