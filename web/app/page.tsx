'use client';

import dynamic from 'next/dynamic';
import { fetchLLM, agUIAdapter, restStorage } from '@openuidev/react-ui';
import { API_URL } from '@/lib/api';
import { qualitestLibrary } from '@/lib/openui/library';
import { useTheme } from '@/lib/theme';

const AgentInterface = dynamic(
  () => import('@openuidev/react-ui').then((m) => m.AgentInterface),
  {
    ssr: false,
    loading: () => (
      <div className="flex h-dvh items-center justify-center text-sm text-muted-foreground">
        Loading…
      </div>
    ),
  },
);

const llm = fetchLLM({
  url: `${API_URL}/agent`,
  streamAdapter: agUIAdapter(),
});

const storage = restStorage({ baseUrl: `${API_URL}/api/threads` });

const starters = [
  {
    displayText: 'Audit a dbt project',
    prompt: 'I want to audit a dbt project from a Git repository.',
  },
  {
    displayText: 'Upload my ETL code',
    prompt: 'I want to upload a folder with my ETL code.',
  },
  {
    displayText: 'Test a live database',
    prompt: 'I have a database connection and want to test the data.',
  },
];

export default function AgentPage() {
  const { theme } = useTheme();
  return (
    <AgentInterface
      llm={llm}
      storage={storage}
      componentLibrary={qualitestLibrary}
      agentName="qualitest"
      starters={starters}
      theme={{ mode: theme }}
    />
  );
}
