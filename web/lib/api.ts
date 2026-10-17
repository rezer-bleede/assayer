import type { FileSelection } from './upload';

export const API_URL = process.env.NEXT_PUBLIC_API_URL ?? 'http://localhost:8010';
export const WS_URL = process.env.NEXT_PUBLIC_WS_URL ?? 'ws://localhost:8010';

async function req<T>(path: string, init?: RequestInit): Promise<T> {
  const res = await fetch(`${API_URL}${path}`, {
    ...init,
    headers: { 'Content-Type': 'application/json', ...(init?.headers ?? {}) },
    cache: 'no-store',
  });
  if (!res.ok) {
    const body = await res.text().catch(() => '');
    throw new Error(parseDetail(body) || `${res.status} ${res.statusText}`);
  }
  return res.json() as Promise<T>;
}

export const api = {
  createSession: () => req<{ session_id: string; question?: Question }>('/api/sessions', { method: 'POST', body: '{}' }),
  messages: (sid: string) => req<{ messages: ChatMessage[] }>(`/api/sessions/${sid}/messages`),
  sessionContext: (sid: string) => req<SessionContext>(`/api/sessions/${sid}/context`),
  sources: () => req<{ sources: SourceKind[]; fields_by_type: Record<string, string[]> }>(
    '/api/connections/sources',
  ),
  testConnection: (spec: Record<string, unknown>) =>
    req<ConnectResponse>(
      '/api/connections/test',
      { method: 'POST', body: JSON.stringify({ spec, persist: true }) },
    ),
  /** Answer a datasource question. Credentials go over REST, never chat history. */
  connectFromUrl: (body: {
    url: string;
    session_id?: string | null;
    requirement_key?: string | null;
    tables?: string[];
    target_tables?: string[];
  }) =>
    req<ConnectResponse>('/api/connections/from-url', {
      method: 'POST',
      body: JSON.stringify(body),
    }),
  analyzeEtl: (payload: Record<string, unknown>) =>
    req<AnalyzeResponse>('/api/etl/analyze', { method: 'POST', body: JSON.stringify(payload) }),
  /** Clone + analyse a repository. Slow, so the caller shows progress. */
  fromRepo: (body: { url: string; ref?: string | null; token?: string | null; session_id?: string | null }) =>
    req<AnalyzeResponse>('/api/etl/from-repo', {
      method: 'POST',
      body: JSON.stringify(body),
    }),
  uploadFiles: async (sel: FileSelection, session_id?: string | null): Promise<AnalyzeResponse> => {
    const form = new FormData();
    sel.files.forEach(({ file, path }) =>
      form.append('files', file, path.split('/').pop() ?? file.name),
    );
    // Relative paths travel separately: multipart `filename` is basename-only,
    // while models/seeds/profiles detection needs the folder structure.
    form.append('paths', JSON.stringify(sel.files.map(({ path }) => path)));
    // A folder pick gets strict filtering (junk is expected); files the user
    // named by hand are taken at face value.
    form.append('mode', sel.mode);
    const res = await fetch(
      `${API_URL}/api/etl/upload${session_id ? `?session_id=${encodeURIComponent(session_id)}` : ''}`,
      { method: 'POST', body: form },
    );
    if (!res.ok) {
      const detail = await res.text().catch(() => '');
      throw new Error(parseDetail(detail) || `${res.status} ${res.statusText}`);
    }
    return res.json();
  },
  plan: (payload: Record<string, unknown>) =>
    req<{ run_id: string; plan: TestPlan }>('/api/runs/plan', {
      method: 'POST',
      body: JSON.stringify(payload),
    }),
  approve: (runId: string, connectionIds: string[]) =>
    req<{ run: RunResult }>(`/api/runs/${runId}/approve`, {
      method: 'POST',
      body: JSON.stringify({ connection_ids: connectionIds, approved: true }),
    }),
  run: (runId: string) => req<RunResult>(`/api/runs/${runId}`),
  runs: () => req<{ runs: RunSummary[] }>('/api/runs'),
};

/** FastAPI puts the human-readable message inside {"detail": "..."}. */
function parseDetail(text: string): string | null {
  if (!text) return null;
  try {
    const parsed = JSON.parse(text);
    return typeof parsed?.detail === 'string' ? parsed.detail : null;
  } catch {
    return text.slice(0, 300);
  }
}

export type ConnectResponse = {
  connection_id: string | null;
  status: ConnectionStatus;
  spec: Record<string, unknown>;
  matched_tables: string[];
  unmatched_tables: string[];
};

export type AnalyzeResponse = {
  analysis: EtlAnalysis;
  required_sources: SourceRequirement[];
};

export type SessionContext = {
  stage: string;
  connections: Record<string, unknown>[];
  etl_id: string | null;
  pending_plan: string | null;
  pending_question: Question | null;
  requirements: SourceRequirement[];
  answered: Record<string, string>;
  last_run_id: string | null;
};

export type QuestionKind = 'git_url' | 'file_upload' | 'paste' | 'connection';

export type QuestionOption = {
  kind: QuestionKind;
  label: string;
  help?: string | null;
  placeholder?: string | null;
  prefill: Record<string, unknown>;
  fields: string[];
  preferred: boolean;
};

export type Question = {
  id: string;
  stage: string;
  title: string;
  prompt: string;
  options: QuestionOption[];
  requirement_key?: string | null;
  skip_allowed: boolean;
};

export type SourceRequirement = {
  key: string;
  tables: string[];
  database?: string | null;
  schema?: string | null;
  engine_hint?: string | null;
  host_hint?: string | null;
  port_hint?: number | null;
  user_hint?: string | null;
  role: 'source' | 'target' | 'mixed';
  origin: string;
  confidence: number;
};

export type EtlAnalysis = {
  id: string;
  language: string;
  label: string | null;
  origin: string;
  repo_url: string | null;
  files: string[];
  sources: string[];
  targets: string[];
  transformations: { kind: string; description: string }[];
  risks: string[];
  data_profiles?: DataProfile[];
  required_sources: SourceRequirement[];
};

/** Column metadata for a data file we profiled (rows never leave the server). */
export type DataProfile = {
  path: string;
  table: string;
  format: string;
  row_count: number | null;
  sampled_rows: number;
  size_bytes: number;
  columns: {
    name: string;
    data_type: string;
    null_ratio: number | null;
    distinct_values: number | null;
    samples: string[];
  }[];
  truncated: boolean;
  note: string | null;
};

export type SourceKind = { type: string; label: string };

export type ConnectionStatus = {
  ok: boolean;
  type: string;
  name: string;
  latency_ms: number | null;
  message: string | null;
  schemas: string[];
  server_version: string | null;
};

export type ChatMessage = {
  id: string;
  role: 'user' | 'assistant' | 'system' | 'tool';
  content: string;
  created_at: string;
  attachments?: { kind: string; ref_id: string; label: string | null }[];
};

export type PlanCheck = {
  id: string;
  name: string;
  kind: string;
  table: string;
  column: string | null;
  severity: 'blocker' | 'critical' | 'warning' | 'info';
  rationale: string | null;
};

export type TestPlan = {
  id: string;
  summary: string | null;
  checks: PlanCheck[];
};

export type CheckResult = {
  check_id: string;
  name: string;
  kind: string;
  table: { schema?: string; name: string };
  column: string | null;
  severity: string;
  status: 'pending' | 'running' | 'passed' | 'failed' | 'error' | 'skipped';
  observed: number | string | null;
  expected: number | string | null;
  failed_rows: number | null;
  sample_rows: Record<string, unknown>[];
  message: string | null;
  duration_ms: number | null;
};

export type QualityScore = {
  overall: number;
  by_severity: Record<string, number>;
  passed: number;
  failed: number;
  errored: number;
  skipped: number;
};

export type RunResult = {
  id: string;
  status: string;
  score: QualityScore;
  results: CheckResult[];
  duration_ms: number | null;
  narrative: string | null;
};

export type RunSummary = {
  id: string;
  status: string;
  score: number;
  passed: number;
  failed: number;
  errored: number;
  checks: number;
  duration_ms: number | null;
  narrative: string | null;
};
