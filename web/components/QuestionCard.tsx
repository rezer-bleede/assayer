'use client';

import { useEffect, useRef, useState } from 'react';
import type { Question, QuestionOption } from '@/lib/api';
import { cn } from '@/lib/utils';

export type QuestionStatus = {
  busy: boolean;
  error?: string | null;
  ok?: string | null;
};

/**
 * Renders whatever the agent is currently asking for. The control is chosen by
 * the server-declared option kind, so the intake order can change without
 * touching this component.
 */
export function QuestionCard({
  question,
  status,
  onSubmitRepo,
  onSubmitFiles,
  onPaste,
  onSubmitConnection,
  onSkip,
}: {
  question: Question;
  status: QuestionStatus;
  onSubmitRepo: (url: string, ref: string | null, token: string | null) => void;
  onSubmitFiles: (files: File[]) => void;
  onPaste: () => void;
  onSubmitConnection: (url: string) => void;
  onSkip?: () => void;
}) {
  const options = question.options ?? [];
  const [activeRaw, setActive] = useState(options[0]?.kind ?? 'git_url');
  // The agent asks several questions in sequence; a stale tab from the previous
  // question would route `submit()` to the wrong handler (e.g. posting a DB URL
  // to the git endpoint). Resolve against the current question's options.
  const active = options.some((o) => o.kind === activeRaw)
    ? activeRaw
    : (options[0]?.kind ?? 'git_url');
  const [url, setUrl] = useState('');
  const [ref, setRef] = useState('');
  const [token, setToken] = useState('');
  const [picked, setPicked] = useState<File[]>([]);
  const [dragging, setDragging] = useState(false);
  const fileRef = useRef<HTMLInputElement>(null);

  // start each new question with a clean form
  useEffect(() => {
    setUrl('');
    setRef('');
    setToken('');
    setPicked([]);
  }, [question.id]);

  const activeOption = options.find((o) => o.kind === active) ?? options[0];
  const canSubmit =
    !status.busy &&
    ((active === 'git_url' && url.trim().length > 5) ||
      (active === 'file_upload' && picked.length > 0) ||
      (active === 'connection' && url.trim().length > 5));

  const submit = () => {
    if (!canSubmit) return;
    if (active === 'git_url') onSubmitRepo(url.trim(), ref.trim() || null, token.trim() || null);
    else if (active === 'file_upload') onSubmitFiles(picked);
    else if (active === 'connection') onSubmitConnection(url.trim());
  };

  return (
    <form
      className="surface animate-fade-up overflow-hidden"
      onSubmit={(e) => {
        e.preventDefault();
        submit();
      }}
    >
      <div className="border-b bg-accent/[0.04] px-4 py-3">
        <div className="flex items-center gap-2">
          <span className="chip bg-accent/12 text-accent">
            {activeOption?.kind === 'connection' ? 'needs a connection' : 'needs your input'}
          </span>
          <h3 className="text-[13.5px] font-semibold">{question.title}</h3>
        </div>
        <p className="mt-1.5 text-[13px] leading-relaxed text-muted">{question.prompt}</p>
      </div>

      {options.length > 1 && (
        <div className="flex gap-1 border-b px-3 py-2">
          {options.map((o) => (
            <button
              key={o.kind}
              type="button"
              onClick={() => setActive(o.kind)}
              className={cn(
                'rounded-lg px-2.5 py-1.5 text-[12.5px] transition-colors',
                active === o.kind
                  ? 'bg-accent/12 font-medium text-accent'
                  : 'text-muted hover:bg-muted/8 hover:text-fg',
              )}
            >
              {o.label}
              {!o.preferred && (
                <span className="ml-1.5 text-[10px] uppercase tracking-wide text-muted/70">
                  least preferred
                </span>
              )}
            </button>
          ))}
        </div>
      )}

      <div className="p-4">
        {activeOption?.kind === 'git_url' && (
          <GitForm
            url={url}
            ref_={ref}
            token={token}
            placeholder={activeOption.placeholder ?? ''}
            busy={status.busy}
            onUrl={setUrl}
            onRef={setRef}
            onToken={setToken}
          />
        )}

        {activeOption?.kind === 'file_upload' && (
          <div
            onDragOver={(e) => {
              e.preventDefault();
              setDragging(true);
            }}
            onDragLeave={() => setDragging(false)}
            onDrop={(e) => {
              e.preventDefault();
              setDragging(false);
              setPicked(Array.from(e.dataTransfer.files));
            }}
            className={cn(
              'rounded-xl border border-dashed px-4 py-6 text-center transition-colors',
              dragging ? 'border-accent bg-accent/[0.05]' : 'border-border',
            )}
          >
            <p className="text-[13px] text-muted">
              Drop <code className="font-mono">.sql</code>,{' '}
              <code className="font-mono">.py</code> or dbt YAML files here
            </p>
            <button type="button" className="btn-ghost mt-3" onClick={() => fileRef.current?.click()}>
              Choose files
            </button>
            <input
              ref={fileRef}
              type="file"
              multiple
              className="hidden"
              accept=".sql,.py,.yml,.yaml,.json,.txt,.ddl,.j2,.jinja"
              onChange={(e) => setPicked(Array.from(e.target.files ?? []))}
            />
            {picked.length > 0 && (
              <ul className="mx-auto mt-3 max-w-md space-y-0.5 text-left">
                {picked.slice(0, 8).map((f) => (
                  <li key={f.name} className="truncate font-mono text-[11.5px] text-muted">
                    {f.name}
                  </li>
                ))}
                {picked.length > 8 && (
                  <li className="text-[11.5px] text-muted">+{picked.length - 8} more</li>
                )}
              </ul>
            )}
          </div>
        )}

        {activeOption?.kind === 'paste' && (
          <div className="text-[13px] leading-relaxed text-muted">
            <p>
              Paste a script into the box below. One self-contained SQL/Python file works;
              anything bigger is better given to me as a repo or a set of files.
            </p>
            <button type="button" className="btn-ghost mt-3" onClick={onPaste}>
              Focus the input
            </button>
          </div>
        )}

        {activeOption?.kind === 'connection' && (
          <ConnectionForm url={url} onUrl={setUrl} prefill={activeOption} busy={status.busy} />
        )}

        {status.error && (
          <p className="mt-3 rounded-lg bg-fail/8 px-3 py-2 text-[12.5px] text-fail">{status.error}</p>
        )}
        {status.ok && (
          <p className="mt-3 rounded-lg bg-pass/8 px-3 py-2 text-[12.5px] text-pass">{status.ok}</p>
        )}
      </div>

      <div className="flex items-center gap-2 border-t px-4 py-2.5">
        <button type="submit" className="btn-primary" disabled={!canSubmit}>
          {status.busy ? 'Working…' : activeOption?.kind === 'connection' ? 'Test & connect' : 'Analyse'}
        </button>
        {question.skip_allowed && onSkip && (
          <button type="button" className="btn-ghost" onClick={onSkip} disabled={status.busy}>
            Skip for now
          </button>
        )}
        <span className="ml-auto text-[11px] text-muted/70">
          {activeOption?.kind === 'git_url'
            ? 'Parsed only — your code is never executed'
            : activeOption?.kind === 'connection'
              ? 'Stored as a secret; never echoed back'
              : ''}
        </span>
      </div>
    </form>
  );
}

function GitForm({
  url,
  ref_,
  token,
  placeholder,
  busy,
  onUrl,
  onRef,
  onToken,
}: {
  url: string;
  ref_: string;
  token: string;
  placeholder: string;
  busy: boolean;
  onUrl: (v: string) => void;
  onRef: (v: string) => void;
  onToken: (v: string) => void;
}) {
  const [showToken, setShowToken] = useState(false);
  return (
    <div className="space-y-2.5">
      <input
        className="field font-mono"
        placeholder={placeholder || 'https://github.com/acme/analytics.git'}
        value={url}
        disabled={busy}
        autoFocus
        onChange={(e) => onUrl(e.target.value)}
      />
      <div className="grid gap-2.5 sm:grid-cols-2">
        <input
          className="field"
          placeholder="branch or tag (optional)"
          value={ref_}
          disabled={busy}
          onChange={(e) => onRef(e.target.value)}
        />
        <div className="relative">
          <input
            className="field pr-14"
            type={showToken ? 'text' : 'password'}
            placeholder="access token (private repos)"
            value={token}
            disabled={busy}
            onChange={(e) => onToken(e.target.value)}
          />
          <button
            type="button"
            onClick={() => setShowToken((s) => !s)}
            className="absolute right-2 top-1/2 -translate-y-1/2 text-[11px] text-muted hover:text-fg"
          >
            {showToken ? 'hide' : 'show'}
          </button>
        </div>
      </div>
      <p className="text-[11.5px] leading-relaxed text-muted">
        HTTPS only. A token is passed to git out-of-band (never in the URL or command line), used
        for this clone, and not written to the chat transcript.
      </p>
    </div>
  );
}

function ConnectionForm({
  url,
  onUrl,
  prefill,
  busy,
}: {
  url: string;
  onUrl: (v: string) => void;
  prefill: QuestionOption;
  busy: boolean;
}) {
  const hints = Object.entries(prefill.prefill ?? {}).filter(
    ([k, v]) => v && !['password', 'token'].includes(k),
  );
  return (
    <div className="space-y-2.5">
      <input
        className="field font-mono"
        placeholder={prefill.placeholder || 'postgresql://user:password@host:5432/database'}
        value={url}
        disabled={busy}
        autoFocus
        onChange={(e) => onUrl(e.target.value)}
      />
      {hints.length > 0 && (
        <div className="flex flex-wrap gap-1.5">
          <span className="text-[11px] text-muted">I expect:</span>
          {hints.map(([k, v]) => (
            <span key={k} className="chip bg-muted/8 text-muted">
              {k}={String(v)}
            </span>
          ))}
        </div>
      )}
    </div>
  );
}