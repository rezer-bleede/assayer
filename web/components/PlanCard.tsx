'use client';

import { useMemo } from 'react';
import type { PlanCheck } from '@/lib/api';
import { cn } from '@/lib/utils';

const FALLBACK_SEV = { label: 'other', cls: 'bg-muted/10 text-muted ring-muted/20' };

const SEV: Record<string, { label: string; cls: string }> = {
  blocker: { label: 'blocker', cls: 'bg-fail/10 text-fail ring-fail/25' },
  critical: { label: 'critical', cls: 'bg-fail/8 text-fail/90 ring-fail/20' },
  warning: { label: 'warning', cls: 'bg-warn/10 text-warn ring-warn/25' },
  info: { label: 'info', cls: 'bg-muted/10 text-muted ring-muted/20' },
};

export function PlanCard({
  checks,
  progress,
  runId,
  onApprove,
  onRegenerate,
  running,
}: {
  checks: PlanCheck[];
  progress?: Record<string, string>;
  runId: string;
  onApprove: () => void;
  onRegenerate: () => void;
  running?: boolean;
}) {
  const grouped = useMemo(() => {
    const g: Record<string, PlanCheck[]> = {};
    for (const c of checks) (g[c.severity] ??= []).push(c);
    const order = ['blocker', 'critical', 'warning', 'info'];
    return order.filter((s) => g[s]?.length).map((s) => [s, g[s]] as const);
  }, [checks]);

  const done = Object.keys(progress ?? {}).length;

  return (
    <div className="surface animate-fade-up overflow-hidden">
      <header className="flex items-center justify-between border-b px-4 py-2.5">
        <div className="flex items-center gap-2">
          <span className="text-[13px] font-semibold">Test plan</span>
          <span className="chip bg-accent/10 text-accent ring-1 ring-accent/25">
            {checks.length} checks
          </span>
          {running && (
            <span className="chip bg-muted/10 text-muted">
              {done}/{checks.length} executed
            </span>
          )}
        </div>
        {!running && (
          <div className="flex gap-2">
            <button className="btn-ghost" onClick={onRegenerate}>
              Regenerate
            </button>
            <button className="btn-primary" onClick={onApprove}>
              Run {checks.length} checks
            </button>
          </div>
        )}
      </header>

      <div className="max-h-80 overflow-y-auto">
        {grouped.map(([sev, list]) => (
          <section key={sev}>
            <h4
              className={cn(
                'sticky top-0 border-b bg-surface/95 px-4 py-1.5 text-[11px] font-semibold uppercase tracking-wider backdrop-blur',
                (SEV[sev] ?? FALLBACK_SEV).cls,
              )}
            >
              {(SEV[sev] ?? FALLBACK_SEV).label} · {list.length}
            </h4>
            <ul>
              {list.map((c) => {
                const st = progress?.[c.id];
                return (
                  <li
                    key={c.id}
                    className="flex items-start gap-3 border-b px-4 py-2 last:border-0 hover:bg-muted/5"
                  >
                    <StatusDot status={st} />
                    <div className="min-w-0 flex-1">
                      <div className="flex items-baseline gap-2">
                        <span className="truncate text-[13px] font-medium">{c.name}</span>
                        <code className="shrink-0 font-mono text-[11px] text-muted">
                          {c.table}
                          {c.column ? `.${c.column}` : ''}
                        </code>
                      </div>
                      {c.rationale && (
                        <p className="mt-0.5 text-[12px] leading-snug text-muted">{c.rationale}</p>
                      )}
                    </div>
                    <span className="chip shrink-0 bg-muted/8 text-muted">{c.kind}</span>
                  </li>
                );
              })}
            </ul>
          </section>
        ))}
      </div>
    </div>
  );
}

function StatusDot({ status }: { status?: string }) {
  if (!status)
    return <span className="mt-1.5 h-2 w-2 shrink-0 rounded-full border-2 border-border" />;
  const map: Record<string, string> = {
    passed: 'bg-pass',
    failed: 'bg-fail',
    error: 'bg-warn',
    skipped: 'bg-muted',
    running: 'bg-accent animate-pulse',
  };
  return <span className={cn('mt-1.5 h-2 w-2 shrink-0 rounded-full', map[status] ?? 'bg-muted')} />;
}