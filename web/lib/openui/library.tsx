'use client';

import { useRef, useState } from 'react';
import { defineComponent, useTriggerAction } from '@openuidev/react-lang';
import { useThreadList } from '@openuidev/react-headless';
import { openuiChatLibrary } from '@openuidev/react-ui/genui-lib';
import { z } from 'zod/v4';
import {
  API_URL,
  api,
  connectFromUrlStream,
  repairPatch,
  type ConnectResponse,
  type RepairPatch,
} from '@/lib/api';
import {
  describeSelection,
  filterUpload,
  fromFileList,
  type FileSelection,
} from '@/lib/upload';
import { cn } from '@/lib/utils';

const tone = z.enum(['neutral', 'success', 'warning', 'danger']);
const numOrStr = z.union([z.string(), z.number(), z.record(z.string(), z.unknown())]).nullable();

const noticeSchema = z.object({
  title: z.string().nullable().optional(),
  body: z.string(),
  tone: tone.optional(),
  bullets: z.array(z.string()).optional(),
  actions: z.array(z.string()).optional(),
});

function NoticeView({ props }: { props: z.infer<typeof noticeSchema> }) {
  const trigger = useTriggerAction();
  const palette = {
    neutral: 'border-border bg-surface',
    success: 'border-pass/30 bg-pass/[0.06]',
    warning: 'border-warn/30 bg-warn/[0.07]',
    danger: 'border-fail/30 bg-fail/[0.06]',
  }[props.tone ?? 'neutral'];
  return (
    <section className={cn('animate-fade-up rounded-xl border px-4 py-3.5', palette)}>
      {props.title && <h3 className="text-[13.5px] font-semibold">{props.title}</h3>}
      <p className="mt-1 whitespace-pre-wrap text-[13px] leading-relaxed text-muted">
        {props.body}
      </p>
      {!!props.bullets?.length && (
        <ul className="mt-2 space-y-1">
          {props.bullets.map((b, i) => (
            <li key={i} className="flex gap-2 text-[12.5px] text-muted">
              <span className="mt-[7px] h-1 w-1 shrink-0 rounded-full bg-current opacity-50" />
              <span>{b}</span>
            </li>
          ))}
        </ul>
      )}
      {!!props.actions?.length && (
        <div className="mt-3 flex flex-wrap gap-1.5">
          {props.actions.map((a) => (
            <button key={a} type="button" className="btn-ghost !px-2.5 !py-1.5 !text-[12.5px]" onClick={() => trigger(a)}>
              {a}
            </button>
          ))}
        </div>
      )}
    </section>
  );
}

const Notice = defineComponent({
  name: 'Notice',
  description:
    'Short status, explanation, welcome text or fallback answer. Use actions for suggested next messages.',
  props: noticeSchema,
  component: NoticeView,
});

const welcomeSchema = z.object({
  title: z.string(),
  body: z.string(),
  examples: z.array(z.string()).optional(),
});

function WelcomeView({ props }: { props: z.infer<typeof welcomeSchema> }) {
  const trigger = useTriggerAction();
  return (
    <section className="animate-fade-up rounded-xl border bg-gradient-to-b from-accent/[0.07] to-transparent px-5 py-6">
      <span className="chip mb-3 bg-accent/12 text-accent">data quality agent</span>
      <h2 className="text-balance text-[24px] font-semibold leading-tight tracking-tight">
        {props.title}
      </h2>
      <p className="mt-2 max-w-2xl text-[14px] leading-relaxed text-muted">{props.body}</p>
      {!!props.examples?.length && (
        <div className="mt-4 flex flex-wrap gap-1.5">
          {props.examples.map((e) => (
            <button key={e} type="button" className="btn-ghost !px-2.5 !py-1.5 !text-[12.5px]" onClick={() => trigger(e)}>
              {e}
            </button>
          ))}
        </div>
      )}
    </section>
  );
}

const Welcome = defineComponent({
  name: 'Welcome',
  description: 'Welcome panel with a short explanation and example prompts.',
  props: welcomeSchema,
  component: WelcomeView,
});

const intakeOptionSchema = z.object({
  kind: z.enum(['git_url', 'file_upload', 'connection', 'web_target']),
  label: z.string(),
  help: z.string().nullable().optional(),
  placeholder: z.string().nullable().optional(),
  requirementKey: z.string().nullable().optional(),
  tables: z.array(z.string()).optional(),
  targetTables: z.array(z.string()).optional(),
  preferred: z.boolean().optional(),
});

const intakeSchema = z.object({
  title: z.string(),
  prompt: z.string(),
  options: z.array(intakeOptionSchema),
  skipAllowed: z.boolean().optional(),
});

type IntakeStatus = {
  busy: boolean;
  ok?: string | null;
  error?: string | null;
  suggestion?: RepairPatch | null;
  attempts?: string[];
};

function applyPatch(url: string, patch: RepairPatch): string | null {
  try {
    const u = new URL(url);
    if (patch.host) u.hostname = patch.host;
    if (patch.port) u.port = String(patch.port);
    if (patch.database) u.pathname = `/${patch.database}`;
    return u.toString();
  } catch {
    return null;
  }
}

function IntakeView({ props }: { props: z.infer<typeof intakeSchema> }) {
  const trigger = useTriggerAction();
  const threadId = useThreadList((s) => s.selectedThreadId);
  const options = props.options ?? [];
  const [active, setActive] = useState(0);
  const option = options[Math.min(active, options.length - 1)];
  const [url, setUrl] = useState('');
  const [ref, setRef] = useState('');
  const [token, setToken] = useState('');
  const [webAuthMode, setWebAuthMode] = useState<'none' | 'basic' | 'login'>('none');
  const [webUsername, setWebUsername] = useState('');
  const [webPassword, setWebPassword] = useState('');
  const [headerName, setHeaderName] = useState('Authorization');
  const [headerValue, setHeaderValue] = useState('');
  const [allowedOrigins, setAllowedOrigins] = useState('');
  const [loginPath, setLoginPath] = useState('/login');
  const [userSelector, setUserSelector] = useState('input[name=username]');
  const [passSelector, setPassSelector] = useState('input[type=password]');
  const [submitSelector, setSubmitSelector] = useState('button[type=submit]');
  const [successSelector, setSuccessSelector] = useState('');
  const [selection, setSelection] = useState<FileSelection | null>(null);
  const [status, setStatus] = useState<IntakeStatus>({ busy: false });
  const dirRef = useRef<HTMLInputElement>(null);
  const fileRef = useRef<HTMLInputElement>(null);

  if (!option) return null;

  const reset = () => setStatus({ busy: false });

  const submitRepo = async () => {
    if (!threadId) return setStatus({ busy: false, error: 'No active thread yet.' });
    setStatus({ busy: true });
    try {
      const res = await api.fromRepo({ url: url.trim(), ref: ref.trim() || null, token: token.trim() || null, session_id: threadId });
      setStatus({ busy: false, ok: `Read ${res.analysis.files.length} file(s).` });
      await trigger('The codebase is ready.');
    } catch (e) {
      setStatus({ busy: false, error: (e as Error).message });
    }
  };

  const submitFiles = async () => {
    if (!threadId) return setStatus({ busy: false, error: 'No active thread yet.' });
    if (!selection) return;
    setStatus({ busy: true });
    try {
      const res = await api.uploadFiles(selection, threadId);
      setStatus({ busy: false, ok: `Read ${res.analysis.files.length} file(s).` });
      await trigger('The codebase is ready.');
    } catch (e) {
      setStatus({ busy: false, error: (e as Error).message });
    }
  };

  const submitConnection = async (target = url) => {
    if (!threadId) return setStatus({ busy: false, error: 'No active thread yet.' });
    setStatus({ busy: true, attempts: [] });
    await connectFromUrlStream(
      {
        url: target.trim(),
        session_id: threadId,
        requirement_key: option.requirementKey ?? null,
        tables: option.tables ?? [],
        target_tables: option.targetTables ?? [],
      },
      {
        onAttempt: (a) =>
          setStatus((s) => ({
            ...s,
            busy: true,
            attempts: [...(s.attempts ?? []), [a.host, a.port].filter((v) => v != null).join(':') + (a.database ? `/${a.database}` : '')],
          })),
        onError: (message) => setStatus((s) => ({ ...s, busy: false, error: message })),
        onResult: (res: ConnectResponse) => {
          const suggestion = repairPatch(res.repair?.suggested);
          if (!res.status.ok || !res.connection_id) {
            setStatus({
              busy: false,
              ok: suggestion ? 'Found a working address. Confirm it below.' : null,
              error: suggestion ? null : (res.repair?.note ?? res.status.message ?? 'Connection failed.'),
              suggestion,
            });
            return;
          }
          setStatus({ busy: false, ok: res.status.message ?? 'Connected.' });
          void trigger('The database is connected.');
        },
      },
    );
  };

  const submitWebTarget = async () => {
    if (!threadId) return setStatus({ busy: false, error: 'No active thread yet.' });
    setStatus({ busy: true });
    try {
      const target = new URL(url.trim());
      const headers = headerValue.trim() ? { [headerName.trim() || 'Authorization']: headerValue.trim() } : {};
      const res = await api.testConnection({
        type: 'web',
        name: target.hostname,
        url: target.toString(),
        auth_mode: webAuthMode,
        auth_username: webUsername || null,
        auth_password: webPassword || null,
        headers,
        options: {
          ...(allowedOrigins.trim() ? { allowed_origins: allowedOrigins.split(',').map((origin) => origin.trim()).filter(Boolean) } : {}),
          ...(webAuthMode === 'login'
            ? { login_path: loginPath, user_selector: userSelector, pass_selector: passSelector, submit_selector: submitSelector, success_selector: successSelector || null }
            : {}),
        },
      }, threadId);
      if (!res.status.ok || !res.connection_id) {
        setStatus({ busy: false, error: res.status.message ?? 'Could not reach the web app.' });
        return;
      }
      setStatus({ busy: false, ok: res.status.message ?? 'Web app connected.' });
      setWebPassword('');
      setHeaderValue('');
      void trigger('The web app is connected.');
    } catch (e) {
      setStatus({ busy: false, error: (e as Error).message });
    }
  };

  const submit = () => {
    if (option.kind === 'git_url') void submitRepo();
    else if (option.kind === 'file_upload') void submitFiles();
    else if (option.kind === 'web_target') void submitWebTarget();
    else void submitConnection();
  };

  const canSubmit =
    !status.busy &&
    ((option.kind === 'git_url' && url.trim().length > 5) ||
      (option.kind === 'file_upload' && (selection?.files.length ?? 0) > 0) ||
      ((option.kind === 'connection' || option.kind === 'web_target') && url.trim().length > 5));

  return (
    <form
      className="animate-fade-up surface overflow-hidden"
      onSubmit={(e) => {
        e.preventDefault();
        submit();
      }}
    >
      <div className="border-b bg-accent/[0.04] px-4 py-3">
        <div className="flex items-center gap-2">
          <span className="chip bg-accent/12 text-accent">needs your input</span>
          <h3 className="text-[13.5px] font-semibold">{props.title}</h3>
        </div>
        <p className="mt-1.5 text-[13px] leading-relaxed text-muted">{props.prompt}</p>
      </div>

      {options.length > 1 && (
        <div className="flex flex-wrap gap-1 border-b px-3 py-2">
          {options.map((o, i) => (
            <button
              key={`${o.kind}-${i}`}
              type="button"
              onClick={() => {
                setActive(i);
                reset();
              }}
              className={cn(
                'rounded-lg px-2.5 py-1.5 text-[12.5px] transition-colors',
                i === active ? 'bg-accent/12 font-medium text-accent' : 'text-muted hover:bg-muted/8 hover:text-fg',
              )}
            >
              {o.label}
              {o.preferred === false && <span className="ml-1.5 text-[10px] uppercase tracking-wide text-muted/70">least preferred</span>}
            </button>
          ))}
        </div>
      )}

      <div className="space-y-3 p-4">
        {option.help && <p className="text-[12.5px] leading-relaxed text-muted">{option.help}</p>}

        {option.kind === 'git_url' && (
          <div className="grid gap-2 sm:grid-cols-[1fr_120px]">
            <input className="field" placeholder={option.placeholder ?? 'https://github.com/org/repo'} value={url} onChange={(e) => setUrl(e.target.value)} disabled={status.busy} />
            <input className="field" placeholder="branch / tag" value={ref} onChange={(e) => setRef(e.target.value)} disabled={status.busy} />
            <input className="field sm:col-span-2" placeholder="optional access token" value={token} onChange={(e) => setToken(e.target.value)} disabled={status.busy} />
          </div>
        )}

        {option.kind === 'file_upload' && (
          <div className="rounded-xl border border-dashed px-4 py-5 text-center">
            <p className="text-[12.5px] leading-relaxed text-muted">
              Select your project folder. SQL, Python, dbt config and small JSON files are read for lineage; data files are profiled only.
            </p>
            <div className="mt-3 flex items-center justify-center gap-2">
              <button type="button" className="btn-primary" onClick={() => dirRef.current?.click()} disabled={status.busy}>
                Choose folder
              </button>
              <button type="button" className="btn-ghost" onClick={() => fileRef.current?.click()} disabled={status.busy}>
                Choose files
              </button>
            </div>
            <input
              ref={dirRef}
              type="file"
              multiple
              className="hidden"
              onChange={(e) => setSelection(filterUpload(fromFileList(e.target.files ?? []), 'folder'))}
              {...({ webkitdirectory: '', directory: '' } as Record<string, string>)}
            />
            <input
              ref={fileRef}
              type="file"
              multiple
              className="hidden"
              accept=".sql,.py,.yml,.yaml,.json,.ddl,.j2,.jinja,.csv,.tsv,.jsonl,.ndjson,.parquet"
              onChange={(e) => setSelection(filterUpload(fromFileList(e.target.files ?? []), 'files'))}
            />
            {selection && <p className="mt-2 text-[12px] font-medium text-pass">{describeSelection(selection)}</p>}
          </div>
        )}

        {option.kind === 'connection' && (
          <div className="space-y-2">
            <input
              className="field"
              placeholder={option.placeholder ?? 'postgresql://user:pass@host:5432/database'}
              value={url}
              onChange={(e) => setUrl(e.target.value)}
              disabled={status.busy}
            />
            {!!option.tables?.length && (
              <p className="text-[11.5px] text-muted">
                Expected tables: <span className="font-mono">{option.tables.join(', ')}</span>
              </p>
            )}
            {!!status.attempts?.length && (
              <p className="font-mono text-[11px] text-muted">tried {status.attempts.join(', ')}</p>
            )}
            {status.suggestion && (
              <button
                type="button"
                className="btn-ghost !px-2.5 !py-1.5 !text-[12px]"
                onClick={() => {
                  const next = applyPatch(url, status.suggestion!);
                  if (next) {
                    setUrl(next);
                    setStatus({ busy: false });
                  }
                }}
              >
                Use suggested address
              </button>
            )}
          </div>
        )}

        {option.kind === 'web_target' && (
          <div className="space-y-2">
            <input className="field" type="url" required placeholder={option.placeholder ?? 'https://app.example.com'} value={url} onChange={(e) => setUrl(e.target.value)} disabled={status.busy} />
            <label className="block text-[11.5px] text-muted">Authentication
              <select className="field mt-1" value={webAuthMode} onChange={(e) => setWebAuthMode(e.target.value as typeof webAuthMode)} disabled={status.busy}>
                <option value="none">No authentication</option>
                <option value="basic">HTTP basic authentication</option>
                <option value="login">Form login</option>
              </select>
            </label>
            {webAuthMode !== 'none' && <div className="grid gap-2 sm:grid-cols-2">
              <input className="field" autoComplete="username" placeholder="Username" value={webUsername} onChange={(e) => setWebUsername(e.target.value)} disabled={status.busy} />
              <input className="field" type="password" autoComplete="current-password" placeholder="Password (not sent in chat)" value={webPassword} onChange={(e) => setWebPassword(e.target.value)} disabled={status.busy} />
            </div>}
            {webAuthMode === 'none' && <div className="grid gap-2 sm:grid-cols-[1fr_2fr]">
              <input className="field" placeholder="Optional header name" value={headerName} onChange={(e) => setHeaderName(e.target.value)} disabled={status.busy} />
              <input className="field" type="password" placeholder="Optional header value" value={headerValue} onChange={(e) => setHeaderValue(e.target.value)} disabled={status.busy} />
            </div>}
            {webAuthMode === 'login' && <details className="rounded-lg border p-2 text-[11.5px] text-muted"><summary className="cursor-pointer">Login form selectors</summary>
              <div className="mt-2 grid gap-2 sm:grid-cols-2">
                <input className="field" placeholder="Login path" value={loginPath} onChange={(e) => setLoginPath(e.target.value)} disabled={status.busy} />
                <input className="field" placeholder="Username selector" value={userSelector} onChange={(e) => setUserSelector(e.target.value)} disabled={status.busy} />
                <input className="field" placeholder="Password selector" value={passSelector} onChange={(e) => setPassSelector(e.target.value)} disabled={status.busy} />
                <input className="field" placeholder="Submit selector" value={submitSelector} onChange={(e) => setSubmitSelector(e.target.value)} disabled={status.busy} />
                <input className="field sm:col-span-2" placeholder="Optional selector visible after login" value={successSelector} onChange={(e) => setSuccessSelector(e.target.value)} disabled={status.busy} />
              </div>
            </details>}
            <details className="rounded-lg border p-2 text-[11.5px] text-muted"><summary className="cursor-pointer">Additional origins</summary>
              <input className="field mt-2" placeholder="https://cdn.example.com, https://api.example.com" value={allowedOrigins} onChange={(e) => setAllowedOrigins(e.target.value)} disabled={status.busy} />
              <p className="mt-1">Only add origins the app needs for assets or API calls. Browser flows remain constrained to the app origin.</p>
            </details>
            <p className="text-[11px] text-muted">Credentials are sent directly to the API and kept out of chat messages.</p>
          </div>
        )}

        {status.ok && <p className="text-[12.5px] text-pass">{status.ok}</p>}
        {status.error && <p className="text-[12.5px] text-fail">{status.error}</p>}

        <div className="flex items-center gap-2">
          <button type="submit" className="btn-primary" disabled={!canSubmit}>
            {status.busy ? 'Working…' : option.kind === 'connection' || option.kind === 'web_target' ? 'Connect' : option.kind === 'file_upload' ? 'Upload & analyze' : 'Analyze repository'}
          </button>
          {props.skipAllowed && (
            <button type="button" className="btn-ghost" onClick={() => trigger('skip')} disabled={status.busy}>
              Skip
            </button>
          )}
        </div>
      </div>
    </form>
  );
}

const IntakeCard = defineComponent({
  name: 'IntakeCard',
  description:
    'Interactive intake form for a git repository, project upload, or database connection. It performs the REST action itself and then continues the conversation.',
  props: intakeSchema,
  component: IntakeView,
});

const planCheckSchema = z.object({
  id: z.string(),
  name: z.string(),
  kind: z.string(),
  table: z.string(),
  url: z.string().nullable().optional(),
  selector: z.string().nullable().optional(),
  expected: z.string().nullable().optional(),
  steps: z.array(z.object({ action: z.string(), selector: z.string().nullable().optional(), value: z.string().nullable().optional() })).optional(),
  params: z.record(z.string(), z.unknown()).optional(),
  column: z.string().nullable().optional(),
  severity: z.enum(['blocker', 'critical', 'warning', 'info']),
  rationale: z.string().nullable().optional(),
});

const planSchema = z.object({
  runId: z.string(),
  summary: z.string().nullable().optional(),
  checks: z.array(planCheckSchema),
  missingTables: z.array(z.string()).optional(),
  followUps: z.array(z.string()).optional(),
});

function PlanView({ props }: { props: z.infer<typeof planSchema> }) {
  const trigger = useTriggerAction();
  const [tab, setTab] = useState<'all' | 'blocker' | 'critical' | 'warning' | 'info'>('all');
  const checks = props.checks ?? [];
  const order = ['blocker', 'critical', 'warning', 'info'] as const;
  const filtered = tab === 'all' ? checks : checks.filter((c) => c.severity === tab);
  return (
    <section className="animate-fade-up surface overflow-hidden">
      <header className="flex flex-wrap items-center gap-2 border-b px-4 py-2.5">
        <span className="text-[13px] font-semibold">Test plan</span>
        <span className="chip bg-accent/10 text-accent ring-1 ring-accent/25">{checks.length} checks</span>
        {props.summary && <span className="text-[12px] text-muted">{props.summary}</span>}
        <div className="ml-auto flex gap-1.5">
          <button type="button" className="btn-ghost !px-2.5 !py-1.5 !text-[12px]" onClick={() => trigger('regenerate the plan')}>
            Regenerate
          </button>
          <button type="button" className="btn-primary !px-2.5 !py-1.5 !text-[12px]" onClick={() => trigger('run the tests')}>
            Run {checks.length} checks
          </button>
        </div>
      </header>
      {!!props.missingTables?.length && (
        <p className="border-b bg-warn/[0.07] px-4 py-2 text-[12px] text-warn">
          Missing required tables: <span className="font-mono">{props.missingTables.join(', ')}</span>
        </p>
      )}
      <div className="flex gap-1 border-b px-3 py-2">
        {(['all', ...order] as const).map((t) => (
          <button
            key={t}
            type="button"
            onClick={() => setTab(t)}
            className={cn(
              'rounded-md px-2 py-1 text-[11.5px] capitalize transition-colors',
              tab === t ? 'bg-accent/12 font-medium text-accent' : 'text-muted hover:bg-muted/8 hover:text-fg',
            )}
          >
            {t}
            {t !== 'all' && <span className="ml-1 opacity-60">{checks.filter((c) => c.severity === t).length}</span>}
          </button>
        ))}
      </div>
      <ul className="max-h-80 overflow-y-auto">
        {filtered.map((c) => (
          <li key={c.id} className="flex items-start gap-3 border-b px-4 py-2 last:border-0">
            <span
              className={cn(
                'mt-0.5 h-2 w-2 shrink-0 rounded-full',
                c.severity === 'blocker' || c.severity === 'critical' ? 'bg-fail' : c.severity === 'warning' ? 'bg-warn' : 'bg-muted',
              )}
            />
            <div className="min-w-0 flex-1">
              <div className="flex flex-wrap items-baseline gap-2">
                <span className="text-[13px] font-medium">{c.name}</span>
                <code className="font-mono text-[11px] text-muted">
                  {c.url || c.table}
                  {c.column ? `.${c.column}` : ''}
                </code>
              </div>
              {c.rationale && <p className="mt-0.5 text-[12px] leading-snug text-muted">{c.rationale}</p>}
              {c.kind === 'flow' && <p className="mt-1 text-[11.5px] text-warn">This approved check will interact with the app.</p>}
              {c.kind === 'visual_diff' && c.params?.rebaseline === true && <p className="mt-1 text-[11.5px] text-warn">This approved check will replace the screenshot baseline.</p>}
              {!!c.steps?.length && <ol className="mt-1 list-inside list-decimal text-[11px] text-muted">{c.steps.map((step, i) => <li key={i}>{step.action}{step.selector ? ` ${step.selector}` : ''}{step.value ? ` → ${step.value}` : ''}</li>)}</ol>}
            </div>
            <span className="chip shrink-0 bg-muted/8 text-muted">{c.kind}</span>
          </li>
        ))}
      </ul>
      {!!props.followUps?.length && (
        <div className="flex flex-wrap gap-1.5 border-t px-4 py-2.5">
          {props.followUps.map((f) => (
            <button key={f} type="button" className="btn-ghost !px-2.5 !py-1.5 !text-[12px]" onClick={() => trigger(f)}>
              {f}
            </button>
          ))}
        </div>
      )}
    </section>
  );
}

const PlanCard = defineComponent({
  name: 'PlanCard',
  description: 'Generated test plan with severity filters and Run/Regenerate actions.',
  props: planSchema,
  component: PlanView,
});

const checkResultSchema = z.object({
  checkId: z.string(),
  name: z.string(),
  kind: z.string(),
  table: z.string(),
  url: z.string().nullable().optional(),
  artifacts: z.array(z.string()).optional(),
  column: z.string().nullable().optional(),
  severity: z.string(),
  status: z.enum(['pending', 'running', 'passed', 'failed', 'error', 'skipped']),
  observed: numOrStr.optional(),
  expected: numOrStr.optional(),
  failedRows: z.number().nullable().optional(),
  message: z.string().nullable().optional(),
});

const reportSchema = z.object({
  runId: z.string(),
  score: z.number(),
  durationMs: z.number().nullable().optional(),
  summary: z.string().nullable().optional(),
  counts: z.object({
    passed: z.number(),
    failed: z.number(),
    errored: z.number(),
    skipped: z.number(),
  }),
  checks: z.array(checkResultSchema),
  followUps: z.array(z.string()).optional(),
});

function ReportView({ props }: { props: z.infer<typeof reportSchema> }) {
  const trigger = useTriggerAction();
  const [tab, setTab] = useState<'failed' | 'passed' | 'all'>('failed');
  const [open, setOpen] = useState<string | null>(null);
  const rows = props.checks ?? [];
  const filtered = tab === 'all' ? rows : rows.filter((r) => (tab === 'failed' ? r.status === 'failed' || r.status === 'error' : r.status === 'passed'));
  const overall = Number.isFinite(props.score) ? props.score : 0;
  const tone = overall >= 95 ? 'text-pass' : overall >= 75 ? 'text-warn' : 'text-fail';
  return (
    <section className="animate-fade-up surface overflow-hidden">
      <header className="flex flex-wrap items-center gap-4 border-b px-4 py-3">
        <div className="flex items-baseline gap-2">
          <span className={cn('text-2xl font-semibold tabular-nums', tone)}>{overall.toFixed(1)}</span>
          <span className="text-xs text-muted">/ 100 quality score</span>
        </div>
        <div className="flex flex-wrap items-center gap-1.5 text-[11px]">
          {([['passed', props.counts.passed, 'text-pass'], ['failed', props.counts.failed, 'text-fail'], ['errored', props.counts.errored, 'text-warn'], ['skipped', props.counts.skipped, 'text-muted']] as const).map(([label, value, cls]) => (
            <span key={label} className="rounded-md bg-muted/8 px-1.5 py-0.5 font-mono">
              <b className={cls}>{value}</b> <span className="text-muted">{label}</span>
            </span>
          ))}
          <span className="rounded-md bg-muted/8 px-1.5 py-0.5 font-mono text-muted">{props.durationMs ?? 0} ms</span>
        </div>
        <div className="ml-auto flex gap-1 rounded-lg bg-muted/8 p-0.5 text-xs">
          {(['failed', 'passed', 'all'] as const).map((t) => (
            <button
              key={t}
              type="button"
              onClick={() => setTab(t)}
              className={cn('rounded-md px-2.5 py-1 capitalize transition-colors', tab === t ? 'bg-surface font-medium shadow-sm' : 'text-muted hover:text-fg')}
            >
              {t}
            </button>
          ))}
        </div>
      </header>
      {props.summary && <p className="border-b px-4 py-2 text-[12.5px] text-muted">{props.summary}</p>}
      <div className="max-h-96 overflow-y-auto">
        {filtered.length === 0 && <p className="px-4 py-8 text-center text-[13px] text-muted">No checks in this view.</p>}
        {filtered.map((r) => {
          const isOpen = open === r.checkId;
          const statusTone =
            r.status === 'passed'
              ? 'text-pass bg-pass/10 ring-pass/25'
              : r.status === 'failed'
                ? 'text-fail bg-fail/10 ring-fail/25'
                : r.status === 'error'
                  ? 'text-warn bg-warn/10 ring-warn/25'
                  : 'text-muted bg-muted/10 ring-muted/20';
          return (
            <div key={r.checkId} className="border-b last:border-0">
              <button type="button" onClick={() => setOpen(isOpen ? null : r.checkId)} className="flex w-full items-start gap-3 px-4 py-2.5 text-left transition-colors hover:bg-muted/5">
                <span className={cn('chip shrink-0 ring-1', statusTone)}>{r.status}</span>
                <div className="min-w-0 flex-1">
                  <div className="flex flex-wrap items-baseline gap-x-2">
                    <span className="text-[13px] font-medium">{r.name}</span>
                    <code className="font-mono text-[11px] text-muted">
                      {r.url || r.table}
                      {r.column ? `.${r.column}` : ''}
                    </code>
                  </div>
                  <p className="mt-0.5 text-[12px] text-muted">{r.message}</p>
                </div>
                <div className="shrink-0 text-right text-[11px] text-muted tabular-nums">
                  <div>obs: {typeof r.observed === 'object' && r.observed ? JSON.stringify(r.observed) : String(r.observed ?? '—')}</div>
                  <div>exp: {String(r.expected ?? '—')}</div>
                </div>
              </button>
              {isOpen && (
                <div className="border-t bg-ink/[0.03] px-4 py-2 text-[11.5px] text-muted dark:bg-white/[0.04]">
                  {r.failedRows ? `${r.failedRows.toLocaleString()} failing row(s).` : 'No failing rows.'}
                  {r.kind === 'flow' && <p className="mt-1">This approved check interacted with the app.</p>}
                  {r.artifacts?.filter((a) => a.endsWith('.png')).map((artifact) => {
                    const name = artifact.split('/').pop();
                    const href = `${API_URL}/api/runs/${encodeURIComponent(props.runId)}/artifacts/${encodeURIComponent(r.checkId)}/${encodeURIComponent(name ?? '')}`;
                    return <a key={artifact} href={href} target="_blank" rel="noreferrer" className="mt-2 block"><img src={href} alt={`${r.name} screenshot`} className="max-h-64 rounded-md border" /></a>;
                  })}
                  {r.kind === 'visual_diff' && typeof r.observed === 'number' && (
                    <div className="mt-2 max-w-xs">
                      <div className="mb-1 flex justify-between"><span>Visual difference</span><span>{(r.observed * 100).toFixed(2)}%</span></div>
                      <div className="h-1.5 overflow-hidden rounded-full bg-muted/15"><div className="h-full bg-accent" style={{ width: `${Math.min(100, Math.max(0, r.observed * 100))}%` }} /></div>
                    </div>
                  )}
                </div>
              )}
            </div>
          );
        })}
      </div>
      {!!props.followUps?.length && (
        <div className="flex flex-wrap gap-1.5 border-t px-4 py-2.5">
          {props.followUps.map((f) => (
            <button key={f} type="button" className="btn-ghost !px-2.5 !py-1.5 !text-[12px]" onClick={() => trigger(f)}>
              {f}
            </button>
          ))}
        </div>
      )}
    </section>
  );
}

const AuditReport = defineComponent({
  name: 'AuditReport',
  description: 'Final quality report with score, counts, and expandable check results.',
  props: reportSchema,
  component: ReportView,
});

const etlSchema = z.object({
  label: z.string().nullable().optional(),
  origin: z.string(),
  language: z.string(),
  files: z.array(z.string()),
  sources: z.array(z.string()),
  targets: z.array(z.string()),
  risks: z.array(z.string()).optional(),
  profiles: z
    .array(
      z.object({
        path: z.string(),
        table: z.string(),
        format: z.string(),
        rowCount: z.number().nullable().optional(),
        columns: z.array(z.object({ name: z.string(), dataType: z.string() })).optional(),
      }),
    )
    .optional(),
});

function EtlView({ props }: { props: z.infer<typeof etlSchema> }) {
  return (
    <section className="animate-fade-up surface overflow-hidden">
      <header className="flex flex-wrap items-center gap-2 border-b px-4 py-2.5">
        <span className="chip bg-pass/10 text-pass">codebase read</span>
        <span className="text-[13px] font-semibold">{props.label ?? props.origin}</span>
        <span className="text-[11.5px] text-muted">{props.language} · {props.files.length} files</span>
      </header>
      <div className="grid gap-3 p-4 sm:grid-cols-2">
        <div>
          <h4 className="text-[11px] font-semibold uppercase tracking-wider text-muted">Sources</h4>
          <p className="mt-1 font-mono text-[12px]">{props.sources.join(', ') || 'none detected'}</p>
        </div>
        <div>
          <h4 className="text-[11px] font-semibold uppercase tracking-wider text-muted">Targets</h4>
          <p className="mt-1 font-mono text-[12px]">{props.targets.join(', ') || 'none detected'}</p>
        </div>
      </div>
      {!!props.profiles?.length && (
        <div className="border-t px-4 py-3">
          <h4 className="text-[11px] font-semibold uppercase tracking-wider text-muted">Profiled data files</h4>
          <ul className="mt-1.5 space-y-1">
            {props.profiles.map((p) => (
              <li key={p.path} className="text-[12px] text-muted">
                <span className="font-mono text-fg">{p.table}</span> · {p.format} · {p.rowCount?.toLocaleString() ?? '?'} rows
                {p.columns?.length ? ` · ${p.columns.length} columns` : ''}
              </li>
            ))}
          </ul>
        </div>
      )}
      {!!props.risks?.length && (
        <div className="border-t bg-warn/[0.05] px-4 py-2.5">
          <h4 className="text-[11px] font-semibold uppercase tracking-wider text-warn">Risks</h4>
          <ul className="mt-1 list-inside list-disc text-[12px] text-muted">
            {props.risks.map((r, i) => <li key={i}>{r}</li>)}
          </ul>
        </div>
      )}
    </section>
  );
}

const EtlCard = defineComponent({
  name: 'EtlCard',
  description: 'Summary of the analyzed ETL codebase and profiled data files.',
  props: etlSchema,
  component: EtlView,
});

const connectionSchema = z.object({
  name: z.string(),
  type: z.string(),
  ok: z.boolean(),
  message: z.string().nullable().optional(),
  matchedTables: z.array(z.string()).optional(),
  unmatchedTables: z.array(z.string()).optional(),
  latencyMs: z.number().nullable().optional(),
});

function ConnectionView({ props }: { props: z.infer<typeof connectionSchema> }) {
  return (
    <section className="animate-fade-up surface px-4 py-3">
      <div className="flex flex-wrap items-center gap-2">
        <span className={cn('chip ring-1', props.ok ? 'bg-pass/10 text-pass ring-pass/25' : 'bg-fail/10 text-fail ring-fail/25')}>
          {props.ok ? 'connected' : 'failed'}
        </span>
        <span className="text-[13px] font-semibold">{props.name}</span>
        <span className="text-[11.5px] text-muted">{props.type}{props.latencyMs != null ? ` · ${props.latencyMs} ms` : ''}</span>
      </div>
      {props.message && <p className="mt-1.5 text-[12.5px] text-muted">{props.message}</p>}
      {!!props.matchedTables?.length && (
        <p className="mt-1 text-[12px] text-muted">
          Matched: <span className="font-mono text-fg">{props.matchedTables.join(', ')}</span>
        </p>
      )}
      {!!props.unmatchedTables?.length && (
        <p className="mt-1 text-[12px] text-warn">
          Not found: <span className="font-mono">{props.unmatchedTables.join(', ')}</span>
        </p>
      )}
    </section>
  );
}

const ConnectionCard = defineComponent({
  name: 'ConnectionCard',
  description: 'Result of a database connection check, including matched tables.',
  props: connectionSchema,
  component: ConnectionView,
});

const repairSchema = z.object({
  note: z.string().nullable().optional(),
  host: z.string().nullable().optional(),
  port: z.number().nullable().optional(),
  database: z.string().nullable().optional(),
});

function RepairView({ props }: { props: z.infer<typeof repairSchema> }) {
  const trigger = useTriggerAction();
  const where = [props.host, props.port].filter((v) => v != null).join(':');
  return (
    <section className="animate-fade-up surface px-4 py-3">
      <span className="chip bg-warn/10 text-warn">connection suggestion</span>
      <p className="mt-1.5 text-[13px]">{props.note ?? 'I found a working address.'}</p>
      {(where || props.database) && (
        <p className="mt-1 font-mono text-[12px] text-muted">{where}{props.database ? ` / ${props.database}` : ''}</p>
      )}
      <button type="button" className="btn-ghost mt-2 !px-2.5 !py-1.5 !text-[12px]" onClick={() => trigger('use it')}>
        Use this address
      </button>
    </section>
  );
}

const RepairCard = defineComponent({
  name: 'RepairCard',
  description: 'A repaired database address the user can accept with a single action.',
  props: repairSchema,
  component: RepairView,
});

export const qualitestLibrary = openuiChatLibrary.extend({
  components: {
    add: [
      { component: Welcome, slots: ['Card.children'] },
      { component: Notice, slots: ['Card.children'] },
      { component: IntakeCard, slots: ['Card.children'] },
      { component: PlanCard, slots: ['Card.children'] },
      { component: AuditReport, slots: ['Card.children'] },
      { component: EtlCard, slots: ['Card.children'] },
      { component: ConnectionCard, slots: ['Card.children'] },
      { component: RepairCard, slots: ['Card.children'] },
    ],
  },
});
