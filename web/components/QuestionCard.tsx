'use client';

import { useEffect, useRef, useState } from 'react';
import {
  describeAttempt,
  type Question,
  type QuestionOption,
  type RepairAttempt,
  type RepairPatch,
} from '@/lib/api';
import {
  describeSelection,
  filterUpload,
  fromDataTransfer,
  fromFileList,
  type FileSelection,
} from '@/lib/upload';
import { cn } from '@/lib/utils';

export type QuestionStatus = {
  busy: boolean;
  error?: string | null;
  ok?: string | null;
  /** live progress: every configuration the agent tried so far */
  attempts?: RepairAttempt[];
  /** a working address the agent found; the user confirms it */
  suggestion?: RepairPatch | null;
};

/** Rewrite host/port/database in the pasted URL, keeping user & password. */
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

function suggestionLabel(patch: RepairPatch): string {
  const where = [patch.host, patch.port].filter((v) => v !== null && v !== undefined).join(':');
  return patch.database ? `${where} / ${patch.database}` : where;
}

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
  onSubmitFiles: (selection: FileSelection) => void;
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
  const [sel, setSel] = useState<FileSelection | null>(null);
  const [dragging, setDragging] = useState(false);
  const fileRef = useRef<HTMLInputElement>(null);
  const dirRef = useRef<HTMLInputElement>(null);

  // start each new question with a clean form
  useEffect(() => {
    setUrl('');
    setRef('');
    setToken('');
    setSel(null);
  }, [question.id]);

  const activeOption = options.find((o) => o.kind === active) ?? options[0];
  const canSubmit =
    !status.busy &&
    ((active === 'git_url' && url.trim().length > 5) ||
      (active === 'file_upload' && (sel?.files.length ?? 0) > 0) ||
      (active === 'connection' && url.trim().length > 5));

  const submit = () => {
    if (!canSubmit) return;
    if (active === 'git_url') onSubmitRepo(url.trim(), ref.trim() || null, token.trim() || null);
    else if (active === 'file_upload' && sel) onSubmitFiles(sel);
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
            onDrop={async (e) => {
              e.preventDefault();
              setDragging(false);
              const items = await fromDataTransfer(e.dataTransfer);
              setSel(filterUpload(items, 'folder'));
            }}
            className={cn(
              'rounded-xl border border-dashed px-4 py-6 text-center transition-colors',
              dragging ? 'border-accent bg-accent/[0.05]' : 'border-border',
            )}
          >
            <p className="text-[13px] text-muted">
              Select or drop your <b>project folder</b>. I read the transformation code and
              config (<code className="font-mono">.sql</code>,{' '}
              <code className="font-mono">.py</code>, dbt YAML + manifest) for lineage, and{' '}
              <b>profile</b> your data files (CSV, JSON, parquet: columns, types, row count)
              instead of loading them. Dependencies, lock files, binaries and generated
              artifacts are skipped.
            </p>
            <div className="mt-3 flex items-center justify-center gap-2">
              <button type="button" className="btn-primary" onClick={() => dirRef.current?.click()}>
                Choose project folder
              </button>
              <button type="button" className="btn-ghost" onClick={() => fileRef.current?.click()}>
                Choose files
              </button>
            </div>
            <input
              ref={dirRef}
              type="file"
              multiple
              className="hidden"
              onChange={(e) => setSel(filterUpload(fromFileList(e.target.files ?? []), 'folder'))}
              {...({ webkitdirectory: '', directory: '' } as Record<string, string>)}
            />
            <input
              ref={fileRef}
              type="file"
              multiple
              className="hidden"
              accept=".sql,.py,.yml,.yaml,.json,.ddl,.j2,.jinja,.csv,.tsv,.jsonl,.parquet"
              onChange={(e) => setSel(filterUpload(fromFileList(e.target.files ?? []), 'files'))}
            />
            {sel && (
              <div className="mx-auto mt-3 max-w-md text-left">
                <p className="text-[12px] font-medium text-pass">{describeSelection(sel)}</p>
                {sel.dataFiles > 0 && (
                  <p className="mt-0.5 text-[11.5px] text-muted">
                    Data files are profiled (schema + row count), not read into the analysis.
                  </p>
                )}
                <ul className="mt-1 space-y-0.5">
                  {sel.files.slice(0, 8).map((nf) => (
                    <li key={nf.path} className="truncate font-mono text-[11.5px] text-muted">
                      {nf.path}
                    </li>
                  ))}
                  {sel.files.length > 8 && (
                    <li className="text-[11.5px] text-muted">+{sel.files.length - 8} more</li>
                  )}
                </ul>
              </div>
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

        {status.attempts && status.attempts.length > 0 && (
          <ul className="mt-3 space-y-1" aria-label="Connection attempts" aria-live="polite">
            {status.attempts.map((a) => (
              <li key={a.n} className="flex items-start gap-2 text-[12px]">
                <span
                  className={cn(
                    'mt-px font-mono text-[11px]',
                    a.outcome === 'ok' ? 'text-pass' : 'text-muted/70',
                  )}
                >
                  {a.outcome === 'ok' ? '✓' : '✗'}
                </span>
                <span className="font-mono text-muted">{describeAttempt(a)}</span>
                {a.note && <span className="text-muted/70">— {a.note}</span>}
              </li>
            ))}
          </ul>
        )}

        {status.suggestion && activeOption?.kind === 'connection' && (
          <div className="mt-3 rounded-lg bg-accent/[0.06] px-3 py-2.5">
            <p className="text-[12.5px] text-fg">
              This address works: <b className="font-mono">{suggestionLabel(status.suggestion)}</b>
            </p>
            <button
              type="button"
              className="btn-primary mt-2"
              disabled={status.busy}
              onClick={() => {
                const next = applyPatch(url, status.suggestion as RepairPatch);
                if (next) setUrl(next);
              }}
            >
              Use it
            </button>
          </div>
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
        autoComplete="off"
        spellCheck={false}
        placeholder={placeholder || 'https://github.com/acme/analytics.git'}
        value={url}
        disabled={busy}
        autoFocus
        onChange={(e) => onUrl(e.target.value)}
      />
      <div className="grid gap-2.5 sm:grid-cols-2">
        <input
          className="field"
          autoComplete="off"
          placeholder="branch or tag (optional)"
          value={ref_}
          disabled={busy}
          onChange={(e) => onRef(e.target.value)}
        />
        <div className="relative">
          <input
            className="field pr-14"
            type={showToken ? 'text' : 'password'}
            // "new-password" (not "off"): Chrome ignores autocomplete=off on
            // password fields and would otherwise offer to store a git PAT.
            autoComplete="new-password"
            spellCheck={false}
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
        // The URL embeds the DB password; keep it out of browser storage.
        autoComplete="off"
        spellCheck={false}
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