import type { FileSelection } from './upload';

export const API_URL = process.env.NEXT_PUBLIC_API_URL ?? 'http://localhost:8010';
export const WS_URL = process.env.NEXT_PUBLIC_WS_URL ?? 'ws://localhost:8010';

async function req<T>(path: string, init?: RequestInit): Promise<T> {
  const res = await fetch(`${API_URL}${path}`, {
    ...init,
    credentials: 'include',
    headers: { 'Content-Type': 'application/json', ...(init?.headers ?? {}) },
    cache: 'no-store',
  });
  if (!res.ok) {
    const body = await res.text().catch(() => '');
    throw new Error(parseDetail(body) || `${res.status} ${res.statusText}`);
  }
  return res.json() as Promise<T>;
}

/** For 204 endpoints: no body to parse. */
async function reqVoid(path: string, init?: RequestInit): Promise<void> {
  const res = await fetch(`${API_URL}${path}`, {
    ...init,
    credentials: 'include',
    headers: { 'Content-Type': 'application/json', ...(init?.headers ?? {}) },
    cache: 'no-store',
  });
  if (!res.ok) {
    const body = await res.text().catch(() => '');
    throw new Error(parseDetail(body) || `${res.status} ${res.statusText}`);
  }
}

export const api = {
  createSession: () => req<{ session_id: string; question?: Question }>('/api/sessions', { method: 'POST', body: '{}' }),
  messages: (sid: string) => req<{ messages: ChatMessage[] }>(`/api/sessions/${sid}/messages`),
  sessionContext: (sid: string) => req<SessionContext>(`/api/sessions/${sid}/context`),
  sources: () => req<{ sources: SourceKind[]; fields_by_type: Record<string, string[]> }>(
    '/api/connections/sources',
  ),
  testConnection: (spec: Record<string, unknown>, session_id?: string | null) =>
    req<ConnectResponse>(
      '/api/connections/test',
      { method: 'POST', body: JSON.stringify({ spec, session_id, persist: true }) },
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
      { method: 'POST', body: form, credentials: 'include' },
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
  projects: () => req<{ projects: Project[] }>('/api/projects'),
  createProject: (name: string, instructions = '') =>
    req<Project>('/api/projects', { method: 'POST', body: JSON.stringify({ name, instructions }) }),
  updateProject: (id: string, patch: { name?: string; instructions?: string }) =>
    req<Project>(`/api/projects/${id}`, { method: 'PATCH', body: JSON.stringify(patch) }),
  deleteProject: (id: string) => reqVoid(`/api/projects/${id}`, { method: 'DELETE' }),
  projectDetail: (id: string) => req<ProjectDetail>(`/api/projects/${id}`),
  integrations: () => req<{ integrations: Integration[] }>('/api/integrations'),
  githubRepositories: (id: string) => req<{ repositories: GitHubRepository[] }>(`/api/integrations/github/${encodeURIComponent(id)}/repositories`),
  projectRepositories: (id: string) => req<{ repositories: ProjectRepository[] }>(`/api/integrations/projects/${encodeURIComponent(id)}/repositories`),
  addProjectRepository: (projectId: string, integrationId: string, repository: string) =>
    req<ProjectRepository>(`/api/integrations/projects/${encodeURIComponent(projectId)}/repositories`, {
      method: 'POST', body: JSON.stringify({ integration_id: integrationId, repository }),
    }),
  removeProjectRepository: (projectId: string, bindingId: string) =>
    reqVoid(`/api/integrations/projects/${encodeURIComponent(projectId)}/repositories/${encodeURIComponent(bindingId)}`, { method: 'DELETE' }),
  disconnectGitHub: (integrationId: string) =>
    reqVoid(`/api/integrations/github/${encodeURIComponent(integrationId)}`, { method: 'DELETE' }),
  runDestinations: (runId: string) =>
    req<{ destinations: IntegrationDestination[] }>(`/api/integrations/runs/${encodeURIComponent(runId)}/destinations`),
  previewGitHubIssue: (payload: { integration_id: string; run_id: string; repository: string; title: string; body: string }) =>
    req<GitHubIssueDraft>('/api/integrations/github/issues/preview', { method: 'POST', body: JSON.stringify(payload) }),
  confirmIntegrationAction: (actionId: string, title: string, body: string) =>
    req<{ status: string; url: string; title: string; number: number }>(`/api/integrations/actions/${encodeURIComponent(actionId)}/confirm`, { method: 'POST', body: JSON.stringify({ title, body }) }),
  connectGitHub: () => { window.location.assign(`${API_URL}/api/integrations/github/connect`); },
  projectFiles: (id: string) => req<{ files: ProjectFile[] }>(`/api/projects/${id}/files`),
  uploadProjectFiles: async (id: string, files: File[]): Promise<{ files: ProjectFile[]; rejected: { name: string; reason: string }[] }> => {
    const form = new FormData();
    files.forEach((f) => form.append('files', f, f.name));
    const res = await fetch(`${API_URL}/api/projects/${encodeURIComponent(id)}/files`, {
      method: 'POST',
      body: form,
      credentials: 'include',
    });
    if (!res.ok) {
      const body = await res.text().catch(() => '');
      throw new Error(parseDetail(body) || `${res.status} ${res.statusText}`);
    }
    return res.json();
  },
  deleteProjectFile: (id: string, fileId: string) =>
    reqVoid(`/api/projects/${id}/files/${encodeURIComponent(fileId)}`, { method: 'DELETE' }),
  /** File a chat into a project, or unfile it when projectId is null. */
  moveThread: (threadId: string, projectId: string | null) =>
    req<{ id: string; projectId: string | null }>('/api/threads/move', {
      method: 'POST',
      body: JSON.stringify({ threadId, projectId }),
    }),
};

export type Integration = { id: string; provider: string; account_id: string; account_name: string; created_at: string };
export type GitHubRepository = { id: string; full_name: string; private: boolean; html_url: string; default_branch: string };
export type ProjectRepository = { id: string; integration_id: string; repository: string; name: string };
export type IntegrationDestination = { integration_id: string; repository: string; account_name: string };
export type GitHubIssueDraft = { id: string; status: string; repository: string; title: string; body: string; run_id: string; run_url: string };

export type FromUrlBody = {
  url: string;
  session_id?: string | null;
  requirement_key?: string | null;
  tables?: string[];
  target_tables?: string[];
};

export type FromUrlStreamHandlers = {
  onAttempt?: (attempt: RepairAttempt) => void;
  onResult: (res: ConnectResponse) => void;
  onError?: (message: string) => void;
};

/**
 * Connect from a pasted URL, watching the repair loop as it runs.
 *
 * A failed connection makes the agent try other addresses (the container
 * boundary, the ClickHouse HTTP port, other databases), which takes seconds —
 * so this streams each attempt instead of showing a spinner, and finishes with
 * the same payload the plain REST call returns.
 */
export async function connectFromUrlStream(
  body: FromUrlBody,
  handlers: FromUrlStreamHandlers,
  signal?: AbortSignal,
): Promise<void> {
  let res: Response;
  try {
    res = await fetch(`${API_URL}/api/connections/from-url`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', Accept: 'text/event-stream' },
      body: JSON.stringify(body),
      cache: 'no-store',
      credentials: 'include',
      signal,
    });
  } catch (e) {
    handlers.onError?.((e as Error).message);
    return;
  }
  if (!res.ok || !res.body) {
    const text = await res.text().catch(() => '');
    handlers.onError?.(parseDetail(text) || `${res.status} ${res.statusText}`);
    return;
  }

  const reader = res.body.getReader();
  const decoder = new TextDecoder();
  let buffer = '';
  let sawResult = false;
  for (;;) {
    const { value, done } = await reader.read();
    if (done) break;
    buffer += decoder.decode(value, { stream: true });
    let split: number;
    while ((split = buffer.indexOf('\n\n')) !== -1) {
      const { event, data } = parseFrame(buffer.slice(0, split));
      buffer = buffer.slice(split + 2);
      if (!event) continue;
      if (event === 'connection.attempt') handlers.onAttempt?.(data as RepairAttempt);
      else if (event === 'result' && data) {
        sawResult = true;
        handlers.onResult(data as ConnectResponse);
      } else if (event === 'error') {
        handlers.onError?.(String((data as { message?: string })?.message ?? 'stream error'));
      }
    }
  }
  if (!sawResult) handlers.onError?.('The connection check ended without a result.');
}

function parseFrame(frame: string): { event: string | null; data: unknown } {
  let event: string | null = null;
  const data: string[] = [];
  for (const line of frame.split('\n')) {
    if (line.startsWith('event:')) event = line.slice(6).trim();
    else if (line.startsWith('data:')) data.push(line.slice(5).replace(/^ /, ''));
  }
  if (!event) return { event: null, data: null };
  try {
    return { event, data: JSON.parse(data.join('\n')) };
  } catch {
    return { event, data: null };
  }
}

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
  /** present when the connection as given failed and the agent searched */
  repair?: RepairReport | null;
};

/** One configuration the agent tried while a connection was failing. */
export type RepairAttempt = {
  n: number;
  source: string;
  host: string | null;
  port: number | null;
  database: string | null;
  outcome: string;
  note: string | null;
};

export type RepairReport = {
  attempted: number;
  exhausted: boolean;
  suggested: Record<string, unknown> | null;
  attempts: RepairAttempt[];
  note: string | null;
};

/** The addressing fields a suggestion changes; applied onto the URL input. */
export type RepairPatch = {
  host?: string | null;
  port?: number | null;
  database?: string | null;
};

export function repairPatch(spec: Record<string, unknown> | null | undefined): RepairPatch | null {
  if (!spec) return null;
  const patch: RepairPatch = {
    host: (spec.host as string) ?? null,
    port: (spec.port as number) ?? null,
    database: (spec.database as string) ?? null,
  };
  return patch.host || patch.port || patch.database ? patch : null;
}

export function describeAttempt(a: RepairAttempt): string {
  const where = [a.host, a.port].filter((v) => v !== null && v !== undefined).join(':');
  return a.database ? `${where}/${a.database}` : where;
}

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

export type Project = {
  id: string;
  name: string;
  instructions: string;
  createdAt: string;
  threadCount?: number;
  fileCount?: number;
};

export type ProjectFile = {
  id: string;
  name: string;
  size: number;
  createdAt?: string;
};

export type ProjectDetail = Project & {
  files: ProjectFile[];
  threads: { id: string; title: string; createdAt: string }[];
};
