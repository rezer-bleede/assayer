'use client';

import dynamic from 'next/dynamic';

const AgentShell = dynamic(() => import('@/components/AgentShell'), {
  ssr: false,
  loading: () => (
    <div className="flex h-dvh items-center justify-center text-sm text-muted">
      Loading…
    </div>
  ),
});

export default function AgentPage() {
  return <AgentShell />;
}
