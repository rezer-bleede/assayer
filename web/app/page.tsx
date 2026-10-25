'use client';

import dynamic from 'next/dynamic';
import { AuthGate } from '@/components/AuthGate';

const AgentShell = dynamic(() => import('@/components/AgentShell'), {
  ssr: false,
  loading: () => (
    <div className="flex h-dvh items-center justify-center text-sm text-muted">
      Loading…
    </div>
  ),
});

export default function AgentPage() {
  return <AuthGate><AgentShell /></AuthGate>;
}
