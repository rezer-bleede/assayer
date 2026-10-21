'use client';

import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { Composer } from '@/components/Composer';
import { MessageBubble, ThinkingStatus, useAutoScroll } from '@/components/Message';
import { PlanCard } from '@/components/PlanCard';
import { QuestionCard, type QuestionStatus } from '@/components/QuestionCard';
import { ResultsCard } from '@/components/ResultsCard';
import { api, connectFromUrlStream, repairPatch, type RunResult } from '@/lib/api';
import type { FileSelection } from '@/lib/upload';
import { useAgentChat } from '@/lib/useAgentChat';
import { useTheme } from '@/lib/theme';
import { cn } from '@/lib/utils';

const STAGES: { key: string; label: string }[] = [
  { key: 'need_etl', label: 'codebase' },
  { key: 'need_sources', label: 'connections' },
  { key: 'ready', label: 'profiling' },
  { key: 'awaiting_approval', label: 'approve' },
  { key: 'running', label: 'execute' },
  { key: 'done', label: 'report' },
];

/** "host:port / database" for a repaired connection the agent is offering. */
function suggestionText(spec: Record<string, unknown>): string {
  const where = [spec.host, spec.port]
    .filter((v) => v !== null && v !== undefined)
    .join(':');
  return spec.database ? `${where} / ${String(spec.database)}` : where;
}

export default function ChatPage() {
  const chat = useAgentChat();
  const [run, setRun] = useState<RunResult | null>(null);
  const [qStatus, setQStatus] = useState<QuestionStatus>({ busy: false });
  const runId = chat.runId;
  const composerRef = useRef<HTMLTextAreaElement>(null);
  const sessionId = chat.sessionId;

  const { ref, onScroll } = useAutoScroll([
    chat.messages.length,
    chat.pending,
    chat.plan,
    chat.question,
    run,
    chat.stream?.content,
    chat.thought,
  ]);

  useEffect(() => {
    if (!runId) {
      setRun(null);
      return;
    }
    let cancelled = false;
    const tick = () =>
      api
        .run(runId)
        .then((r) => {
          if (!cancelled) setRun(r);
        })
        .catch(() => undefined);
    tick();
    const timer = setInterval(tick, 2000);
    return () => {
      cancelled = true;
      clearInterval(timer);
    };
  }, [runId]);

  useEffect(() => {
    setQStatus({ busy: false });
  }, [chat.question?.id]);

  const requirementTables = useCallback(() => {
    const key = chat.question?.requirement_key;
    const req = key
      ? chat.analysis?.required_sources.find((r) => r.key === key)
      : undefined;
    if (!req) return { tables: [] as string[], targets: [] as string[] };
    const targetSet = new Set(chat.analysis?.targets ?? []);
    return {
      tables: req.tables,
      targets: req.tables.filter((t) => targetSet.has(t) || req.role !== 'source'),
    };
  }, [chat.question, chat.analysis]);

  const handleRepo = useCallback(
    async (url: string, ref_: string | null, token: string | null) => {
      setQStatus({ busy: true });
      try {
        const res = await api.fromRepo({ url, ref: ref_, token, session_id: sessionId });
        setQStatus({
          busy: false,
          ok: `Cloned and read ${res.analysis.files.length} file(s).`,
          error: null,
        });
        chat.sendAction('etl_provided', { etl_id: res.analysis.id });
      } catch (e) {
        setQStatus({ busy: false, ok: null, error: (e as Error).message });
      }
    },
    [sessionId, chat],
  );

  const handleFiles = useCallback(
    async (sel: FileSelection) => {
      setQStatus({ busy: true });
      try {
        const res = await api.uploadFiles(sel, sessionId);
        const profiles = res.analysis.data_profiles ?? [];
        const profiled =
          profiles.length > 0
            ? ` Profiled ${profiles.length} data file(s): ` +
              profiles
                .slice(0, 2)
                .map((p) => `${p.path} (${(p.row_count ?? 0).toLocaleString()} rows)`)
                .join(', ') +
              (profiles.length > 2 ? '…' : '') +
              '.'
            : '';
        setQStatus({
          busy: false,
          ok: `Read ${res.analysis.files.length} file(s).${profiled}`,
          error: null,
        });
        chat.sendAction('etl_provided', { etl_id: res.analysis.id });
      } catch (e) {
        setQStatus({ busy: false, ok: null, error: (e as Error).message });
      }
    },
    [sessionId, chat],
  );

  const handleConnection = useCallback(
    async (url: string) => {
      const { tables, targets } = requirementTables();
      const requirementKey = chat.question?.requirement_key ?? null;
      setQStatus({ busy: true, attempts: [] });
      await connectFromUrlStream(
        {
          url,
          session_id: sessionId,
          requirement_key: requirementKey,
          tables,
          target_tables: targets,
        },
        {
          onAttempt: (a) =>
            setQStatus((s) => ({ ...s, busy: true, attempts: [...(s.attempts ?? []), a] })),
          onError: (message) =>
            setQStatus((s) => ({ ...s, busy: false, ok: null, error: message })),
          onResult: (res) => {
            const suggestion = repairPatch(res.repair?.suggested);
            // a repaired connection is never persisted server-side: it is a
            // suggestion the user confirms, so the form offers it explicitly
            if (!res.status.ok || !res.connection_id) {
              setQStatus((s) => ({
                ...s,
                busy: false,
                ok: suggestion ? 'Found a working address — confirm it below.' : null,
                error: suggestion
                  ? null
                  : (res.repair?.note ?? res.status.message ?? 'Connection failed.'),
                suggestion,
              }));
              return;
            }
            const note = res.unmatched_tables.length
              ? `Connected. Heads up: ${res.unmatched_tables.slice(0, 3).join(', ')} not found there.`
              : `Connected — all ${res.matched_tables.length} expected table(s) present.`;
            setQStatus({ busy: false, ok: note, error: null });
            chat.sendAction('connection_provided', {
              connection_id: res.connection_id,
              requirement_key: requirementKey,
            });
          },
        },
      );
    },
    [requirementTables, sessionId, chat],
  );

  const messages = chat.messages;
  const transcript = useMemo(
    () =>
      messages
        .filter((m) => m.role === 'user' || m.role === 'assistant')
        .map((m) => ({ id: m.id, role: m.role, content: m.content, created_at: m.created_at })),
    [messages],
  );

  const showPlan = chat.plan !== null;
  const showResults = run !== null && run.status === 'completed';
  const empty = transcript.length === 0;
  const stageIdx = STAGES.findIndex((s) => s.key === chat.stage);

  return (
    <div className="flex h-dvh flex-col" id="app">
      <Header connections={chat.connections.length} stageIdx={stageIdx} connected={chat.connected} onReset={chat.reset} />

      <main ref={ref} onScroll={onScroll} className="flex-1 overflow-y-auto">
        <div className="mx-auto flex w-full max-w-5xl flex-col gap-3.5 px-6 py-8">
          {empty && (
            <section className="animate-fade-up pb-1 pt-4" aria-label="Welcome">
              <h2 className="text-balance text-[26px] font-semibold leading-tight tracking-tight">
                Which codebase should I audit?
              </h2>
              <p className="mt-2 max-w-2xl text-[14px] leading-relaxed text-muted">
                I read your transformations, work out which databases they touch, and then ask you
                for exactly those connections — nothing else. Credentials are stored as secrets and
                never shown back.
              </p>
            </section>
          )}

          {transcript.map((m) => (
            <article key={m.id} role="chat-message" aria-label={m.role}>
              <MessageBubble key={m.id} message={m} />
            </article>
          ))}

          {/* in-flight assistant reply: sits exactly where the finalized
              message will land, so completion never reflows the transcript */}
          {chat.stream && chat.stream.content !== '' && (
            <article role="chat-message" aria-label="assistant">
              <MessageBubble
                key={chat.stream.id}
                message={{
                  id: chat.stream.id,
                  role: 'assistant',
                  content: chat.stream.content,
                  created_at: '',
                }}
                streaming
              />
            </article>
          )}

          {chat.question && (
            <section aria-label="Agent question">
              <QuestionCard
                question={chat.question}
                status={qStatus}
                onSubmitRepo={handleRepo}
                onSubmitFiles={handleFiles}
                onSubmitConnection={handleConnection}
                onPaste={() => composerRef.current?.focus()}
                onSkip={() => {
                  chat.sendAction('skip_question', { requirement_key: chat.question?.requirement_key });
                  setQStatus({ busy: false });
                }}
              />
            </section>
          )}

          {chat.connectionSuggestion && (
            <section
              aria-label="Repaired connection"
              className="surface animate-fade-up px-4 py-3"
            >
              <p className="text-[13px]">
                That address didn&rsquo;t work, but I found one that does —{' '}
                <b className="font-mono">
                  {suggestionText(chat.connectionSuggestion)}
                </b>
                .
              </p>
              <button
                type="button"
                className="btn-primary mt-2"
                onClick={() => chat.sendAction('accept_connection', {})}
              >
                Use it
              </button>
            </section>
          )}

          {showPlan && (            <section aria-label="Test plan">
              <PlanCard
                runId={chat.planRunId ?? ''}
                checks={chat.plan ?? []}
                progress={chat.progress}
                running={Object.keys(chat.progress).length > 0}
                onApprove={() => chat.send('run the tests', 'approve')}
                onRegenerate={() => chat.send('regenerate the plan with different thresholds')}
              />
            </section>
          )}

          {chat.pending && <ThinkingStatus thought={chat.thought} />}

          {showResults && run && <section aria-label="Run results"><ResultsCard run={run} /></section>}

          {showResults && run && (
            <div className="px-1 text-[11.5px] text-muted">
              Ask a follow-up — <em>&ldquo;why did the foreign key check fail?&rdquo;</em> or{' '}
              <em>&ldquo;add a uniqueness check on customer_id&rdquo;</em>.
            </div>
          )}
        </div>
      </main>

      <Composer
        ref={composerRef}
        onSend={(t) => chat.send(t)}
        disabled={chat.pending}
        placeholder={composerPlaceholder(chat.stage, Boolean(chat.question))}
        hint={
          chat.stage === 'need_etl'
            ? 'Or paste a single script here — a repo or file set gives me much more to work with.'
            : chat.stage === 'need_sources'
              ? 'You can also paste a connection URL directly, like postgresql://user:password@host:5432/db'
              : null
        }
      />

      {chat.error && (
        <div className="fixed bottom-28 left-1/2 -translate-x-1/2 rounded-lg bg-fail px-3 py-1.5 text-xs text-white shadow-lg">
          {chat.error}
        </div>
      )}
    </div>
  );
}

function composerPlaceholder(stage: string, hasQuestion: boolean) {
  if (stage === 'need_etl') return 'Paste your ETL script here…';
  if (stage === 'need_sources' && hasQuestion)
    return 'Or paste a connection URL directly, like postgresql://user:password@host:5432/db';
  if (stage === 'awaiting_approval') return 'Ask for changes, or say "run the tests"…';
  if (stage === 'done') return 'Ask about a failure, or request more checks…';
  return 'Message the agent…';
}

function Header({
  connections,
  stageIdx,
  connected,
  onReset,
}: {
  connections: number;
  stageIdx: number;
  connected: boolean;
  onReset: () => void;
}) {
  return (
    <header className="sticky top-0 z-10 border-b bg-bg/85 backdrop-blur">
      <div className="mx-auto flex w-full max-w-5xl items-center gap-3 px-6 py-2.5">
        <Logo />
        <div className="min-w-0 flex-1">
          <h1 className="truncate text-[13px] font-semibold">qualitest</h1>
          <p className="truncate text-[11px] text-muted">agentic data quality auditing</p>
        </div>
        <ol className="hidden items-center gap-1 lg:flex">
          {STAGES.map((s, i) => (
            <li
              key={s.key}
              className={cn(
                'rounded-md px-1.5 py-0.5 font-mono text-[10px] uppercase tracking-wide transition-colors',
                i === stageIdx
                  ? 'bg-accent/12 font-semibold text-accent'
                  : i < stageIdx
                    ? 'text-pass/70'
                    : 'text-muted/50',
              )}
            >
              {s.label}
            </li>
          ))}
        </ol>
        {connections > 0 && (
          <span className="hidden chip bg-pass/10 text-pass sm:inline-flex">
            {connections} connected
          </span>
        )}
        <span
          className={cn('h-2 w-2 rounded-full', connected ? 'bg-pass' : 'bg-muted/40')}
          title={connected ? 'connected' : 'connecting'}
        />
        <ThemeToggle />
        <button onClick={onReset} className="btn-ghost px-2 py-1 text-[11px]">
          New session
        </button>
      </div>
    </header>
  );
}

/** Light/dark switch. The icon shows what clicking will switch *to*. */
function ThemeToggle() {
  const { theme, toggle } = useTheme();
  const next = theme === 'dark' ? 'light' : 'dark';
  return (
    <button
      type="button"
      onClick={toggle}
      className="btn-ghost h-7 w-7 p-0"
      title={`Switch to ${next} mode`}
      aria-label={`Switch to ${next} mode`}
    >
      {theme === 'dark' ? (
        <svg width="14" height="14" viewBox="0 0 16 16" fill="none" aria-hidden="true">
          <circle cx="8" cy="8" r="3.1" stroke="currentColor" strokeWidth="1.5" />
          <path
            d="M8 1v1.6M8 13.4V15M15 8h-1.6M2.6 8H1M12.9 3.1l-1.1 1.1M4.2 11.8l-1.1 1.1M12.9 12.9l-1.1-1.1M4.2 4.2 3.1 3.1"
            stroke="currentColor"
            strokeWidth="1.5"
            strokeLinecap="round"
          />
        </svg>
      ) : (
        <svg width="14" height="14" viewBox="0 0 16 16" fill="none" aria-hidden="true">
          <path
            d="M13.5 9.6A5.8 5.8 0 0 1 6.4 2.5 5.9 5.9 0 1 0 13.5 9.6Z"
            stroke="currentColor"
            strokeWidth="1.5"
            strokeLinejoin="round"
          />
        </svg>
      )}
    </button>
  );
}

function Logo() {
  return (
    <div className="grid h-7 w-7 shrink-0 place-items-center rounded-lg bg-accent/12 ring-1 ring-accent/25">
      <svg width="15" height="15" viewBox="0 0 16 16" fill="none">
        <path
          d="M3 12.5 6 7l3 3 4-6.5"
          stroke="rgb(var(--accent))"
          strokeWidth="1.7"
          strokeLinecap="round"
          strokeLinejoin="round"
        />
        <circle cx="13" cy="3.5" r="1.6" fill="rgb(var(--accent))" />
      </svg>
    </div>
  );
}
