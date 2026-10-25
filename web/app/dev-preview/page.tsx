'use client';

import { PlanCard } from '@/components/PlanCard';
import { ResultsCard } from '@/components/ResultsCard';
import type { PlanCheck, RunResult } from '@/lib/api';

const sampleChecks: PlanCheck[] = [
  {
    id: 'chk_1',
    name: 'analytics.daily_revenue.customer_id referential integrity',
    kind: 'foreign_key',
    severity: 'critical',
    table: 'analytics.daily_revenue',
    column: 'customer_id',
    rationale: 'Verify all revenue records belong to valid customers in raw.customers'
  },
  {
    id: 'chk_2',
    name: 'raw.orders.order_id uniqueness',
    kind: 'unique',
    severity: 'critical',
    table: 'raw.orders',
    column: 'order_id',
    rationale: 'Primary key order_id must be unique across all raw order ingest records'
  },
  {
    id: 'chk_3',
    name: 'raw.customers.email pattern validation',
    kind: 'pattern',
    severity: 'warning',
    table: 'raw.customers',
    column: 'email',
    rationale: 'Ensure customer email addresses conform to standard RFC 5322 format'
  },
  {
    id: 'chk_4',
    name: 'raw.orders.created_at freshness SLA (<24h)',
    kind: 'freshness',
    severity: 'critical',
    table: 'raw.orders',
    column: 'created_at',
    rationale: 'Check that maximum order creation timestamp is within 24h SLA'
  },
  {
    id: 'chk_5',
    name: 'raw.orders.amount non-negative constraint',
    kind: 'min_value',
    severity: 'info',
    table: 'raw.orders',
    column: 'amount',
    rationale: 'Verify transaction amounts are greater than or equal to 0'
  }
];

const sampleRunResult: RunResult = {
  id: 'run_demo_101',
  status: 'completed',
  duration_ms: 1240,
  narrative: 'Completed test run analysis.',
  score: {
    overall: 85.31,
    passed: 25,
    failed: 3,
    errored: 0,
    skipped: 1,
    by_severity: { critical: 2, warning: 1 }
  },
  results: [
    {
      check_id: 'chk_1',
      name: 'analytics.daily_revenue.customer_id referential integrity',
      kind: 'foreign_key',
      status: 'failed',
      severity: 'critical',
      table: { schema: 'analytics', name: 'daily_revenue' },
      column: 'customer_id',
      message: 'Found 1 orphan row with customer_id key 99999 not present in raw.customers',
      observed: 1,
      expected: 0,
      failed_rows: 1,
      duration_ms: 45,
      sample_rows: [{ order_id: 1042, customer_id: 99999, amount: 250.00 }]
    },
    {
      check_id: 'chk_2',
      name: 'raw.orders.order_id uniqueness',
      kind: 'unique',
      status: 'failed',
      severity: 'critical',
      table: { schema: 'raw', name: 'orders' },
      column: 'order_id',
      message: 'Duplicate primary key detected: order_id 5012 appears 2 times',
      observed: 1,
      expected: 0,
      failed_rows: 1,
      duration_ms: 32,
      sample_rows: [{ order_id: 5012, count: 2 }]
    },
    {
      check_id: 'chk_3',
      name: 'raw.customers.email pattern validation',
      kind: 'pattern',
      status: 'failed',
      severity: 'warning',
      table: { schema: 'raw', name: 'customers' },
      column: 'email',
      message: 'Invalid email format detected in 1 row: "john.doe@invalid_domain"',
      observed: 1,
      expected: 0,
      failed_rows: 1,
      duration_ms: 28,
      sample_rows: [{ customer_id: 88, email: 'john.doe@invalid_domain' }]
    },
    {
      check_id: 'chk_4',
      name: 'raw.orders.created_at freshness SLA (<24h)',
      kind: 'freshness',
      status: 'passed',
      severity: 'critical',
      table: { schema: 'raw', name: 'orders' },
      column: 'created_at',
      message: 'Data freshness within SLA (latest record 2.4 hours old)',
      observed: '2.4 hours',
      expected: '< 24 hours',
      failed_rows: 0,
      duration_ms: 18,
      sample_rows: []
    },
    {
      check_id: 'chk_5',
      name: 'raw.orders.amount non-negative constraint',
      kind: 'min_value',
      status: 'passed',
      severity: 'info',
      table: { schema: 'raw', name: 'orders' },
      column: 'amount',
      message: 'All values >= 0',
      observed: 0,
      expected: 0,
      failed_rows: 0,
      duration_ms: 15,
      sample_rows: []
    }
  ]
};

export default function DevPreviewPage() {
  return (
    <div className="min-h-screen bg-bg p-8 text-fg space-y-8 max-w-5xl mx-auto dark">
      <header className="border-b border-border pb-4">
        <div className="flex items-center gap-3">
          <div className="h-3 w-3 rounded-full bg-accent animate-pulse" />
          <h1 className="text-xl font-bold tracking-tight text-fg">Assayer QA Agent Live Preview Session</h1>
        </div>
        <p className="text-xs text-muted mt-1">Real React components rendered with Next.js 14</p>
      </header>

      <div className="space-y-3">
        <div className="text-xs font-mono text-accent uppercase tracking-wider">Plan Phase</div>
        <PlanCard
          checks={sampleChecks}
          runId="run_demo_101"
          onApprove={() => {}}
          onRegenerate={() => {}}
        />
      </div>

      <div className="space-y-3">
        <div className="text-xs font-mono text-accent uppercase tracking-wider">Results Phase</div>
        <ResultsCard run={sampleRunResult} />
      </div>
    </div>
  );
}
