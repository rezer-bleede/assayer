'use client';

import { useMemo, useState } from 'react';
import type { CheckResult, RunResult } from '@/lib/api';
import { cn } from '@/lib/utils';

const TONE: Record<string, string> = {
  passed: 'text-pass bg-pass/10 ring-pass/25',
  failed: 'text-fail bg-fail/10 ring-fail/25',
  error: 'text-warn bg-warn/10 ring-warn/25',
  skipped: 'text-muted bg-muted/10 ring-muted/20',
  running: 'text-accent bg-accent/10 ring-accent/25',
  pending: 'text-muted bg-muted/10 ring-muted/20',
};

export function ResultsCard({ run }: { run: RunResult }) {
  const [tab, setTab] = useState<'all' | 'failed' | 'passed'>('failed');
  const [open, setOpen] = useState<string | null>(null);

  const filtered = useMemo(() => {
    const rows = run?.results ?? [];
    if (tab === 'all') return rows;
    return rows.filter((r) =>
      tab === 'failed' ? r.status === 'failed' || r.status === 'error' : r.status === 'passed',
    );
  }, [run, tab]);

  // Defensive: a partially-hydrated run must not blank the whole page.
  const score = run?.score ?? { overall: 0, passed: 0, failed: 0, errored: 0, skipped: 0, by_severity: {} };
  const overall = Number.isFinite(score.overall) ? score.overall : 0;
  const tone = overall >= 95 ? 'text-pass' : overall >= 75 ? 'text-warn' : 'text-fail';

  return (
    <div className="surface animate-fade-up overflow-hidden">
      <header className="flex flex-wrap items-center gap-4 border-b px-4 py-3">
        <div className="flex items-baseline gap-2">
          <span className={cn('text-2xl font-semibold tabular-nums', tone)}>
            {overall.toFixed(1)}
          </span>
          <span className="text-xs text-muted">/ 100 QA score</span>
        </div>
        <div className="flex flex-wrap items-center gap-1.5 text-[11px]">
          <Stat label="passed" value={score.passed} cls="text-pass" />
          <Stat label="failed" value={score.failed} cls="text-fail" />
          <Stat label="errored" value={score.errored} cls="text-warn" />
          <Stat label="skipped" value={score.skipped} cls="text-muted" />
          <Stat label="ms" value={run.duration_ms ?? 0} cls="text-muted" />
        </div>
        <div className="ml-auto flex gap-1 rounded-lg bg-muted/8 p-0.5 text-xs">
          {(['failed', 'passed', 'all'] as const).map((t) => (
            <button
              key={t}
              onClick={() => setTab(t)}
              className={cn(
                'rounded-md px-2.5 py-1 capitalize transition-colors',
                tab === t ? 'bg-surface font-medium shadow-sm' : 'text-muted hover:text-fg',
              )}
            >
              {t}
            </button>
          ))}
        </div>
      </header>

      <div className="max-h-96 overflow-y-auto">
        {filtered.length === 0 && (
          <p className="px-4 py-8 text-center text-[13px] text-muted">No checks in this view.</p>
        )}
        {filtered.map((r) => (
          <ResultRow key={r.check_id} r={r} open={open === r.check_id} onToggle={() => setOpen(open === r.check_id ? null : r.check_id)} />
        ))}
      </div>
    </div>
  );
}

function ResultRow({ r, open, onToggle }: { r: CheckResult; open: boolean; onToggle: () => void }) {
  return (
    <div className="border-b last:border-0">
      <button
        onClick={onToggle}
        className="flex w-full items-start gap-3 px-4 py-2.5 text-left transition-colors hover:bg-muted/5"
      >
        <span className={cn('chip shrink-0 ring-1', TONE[r.status])}>{r.status}</span>
        <div className="min-w-0 flex-1">
          <div className="flex flex-wrap items-baseline gap-x-2">
            <span className="text-[13px] font-medium">{r.name}</span>
            <code className="font-mono text-[11px] text-muted">
              {r.table?.schema ? `${r.table.schema}.` : ''}
              {r.table?.name ?? '?'}
              {r.column ? `.${r.column}` : ''}
            </code>
          </div>
          <p className="mt-0.5 text-[12px] text-muted">{r.message}</p>
        </div>
        <div className="shrink-0 text-right text-[11px] text-muted tabular-nums">
          <div>obs: {String(r.observed ?? '\u2014')}</div>
          <div>exp: {String(r.expected ?? '\u2014')}</div>
        </div>
      </button>

      {open && (
        <div className="border-t bg-ink/[0.03] px-4 py-2 dark:bg-white/[0.04]">
          {r.sample_rows?.length ? (
            <pre className="overflow-x-auto font-mono text-[11px] leading-relaxed">
              {JSON.stringify(r.sample_rows.slice(0, 5), null, 2)}
            </pre>
          ) : (
            <p className="text-[11px] text-muted">
              {r.failed_rows
                ? `${r.failed_rows.toLocaleString()} failing row(s); sample unavailable for this check type.`
                : 'No failing rows.'}
            </p>
          )}
        </div>
      )}
    </div>
  );
}

function Stat({ label, value, cls }: { label: string; value: number; cls: string }) {
  return (
    <span className="rounded-md bg-muted/8 px-1.5 py-0.5 font-mono">
      <b className={cls}>{value}</b> <span className="text-muted">{label}</span>
    </span>
  );
}
