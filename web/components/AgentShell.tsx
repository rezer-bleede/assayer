'use client';

/**
 * The chat surface, recomposed to add Projects (ChatGPT/Claude-style).
 *
 * The library's default sidebar is replaced via `AgentInterface.Sidebar`
 * (slot children are extracted by the parent), giving us: an "All chats"
 * view that behaves exactly like before, a Projects list whose chats are
 * filtered per project, a settings route per project, and a one-time nudge
 * to pick a project for new chats. Threads carry `projectId` straight
 * through `GET /api/threads/get` — the storage layer never drops fields.
 */

import { AgentInterface, agUIAdapter, fetchLLM } from '@openuidev/react-ui';
import { useEffect, useState } from 'react';
import { API_URL } from '@/lib/api';
import { assayerLibrary } from '@/lib/openui/library';
import { projectAwareStorage, useProjects } from '@/lib/projects';
import { useTheme } from '@/lib/theme';
import { ProjectsSidebar } from '@/components/ProjectsSidebar';
import { ProjectSettings } from '@/components/ProjectSettings';
import { ProjectNudge } from '@/components/ProjectNudge';

const llm = fetchLLM({
  url: `${API_URL}/agent`,
  streamAdapter: agUIAdapter(),
  fetch: (input, init) => fetch(input, { ...init, credentials: 'include' }),
});

const threadStorage = projectAwareStorage();

const starters = [
  {
    displayText: 'Validate my data',
    prompt: 'I want to validate a data source or files and check for quality issues.',
  },
  {
    displayText: 'Test my ETL pipeline',
    prompt: 'I want to test an ETL pipeline and verify its transformations.',
  },
  {
    displayText: 'Test my web app',
    prompt: 'I want to test a live web app for availability, browser errors, and key page behavior.',
  },
  {
    displayText: 'Check my UI for regressions',
    prompt: 'I want to check my app UI for interaction issues and visual regressions.',
  },
];

export default function AgentShell() {
  const { theme, choice, setChoice } = useTheme();
  const [navPath, setNavPath] = useState<string | undefined>(undefined);
  const [settingsProjectId, setSettingsProjectId] = useState<string | null>(null);
  const loadProjects = useProjects((s) => s.load);

  useEffect(() => {
    void loadProjects();
  }, [loadProjects]);

  return (
    <AgentInterface
      llm={llm}
      storage={threadStorage}
      componentLibrary={assayerLibrary}
      agentName="assayer"
      logoUrl="/logo.svg"
      starters={starters}
      starterVariant="card"
      theme={{ mode: theme }}
      path={navPath}
      onNavigate={setNavPath}
    >
      <AgentInterface.Welcome
        title="Confidence in every release."
        description="Test your data, pipelines, and product with an AI QA partner that turns questions into clear, actionable results."
        image={{ url: '/logo.svg' }}
        starters={starters}
        starterVariant="card"
        startersTabLabel="What would you like to test?"
        glowAnimation
      />
      <AgentInterface.Sidebar>
        <ProjectsSidebar
          theme={theme}
          onThemeChange={(next) => setChoice(next)}
          themeChoice={choice}
          onThemeChoiceChange={setChoice}
          onOpenSettings={(id) => {
            setSettingsProjectId(id);
          }}
        />
      </AgentInterface.Sidebar>
      {settingsProjectId ? (
        <AgentInterface.Route path={`/project/${settingsProjectId}`}>
          <ProjectSettings
            projectId={settingsProjectId}
            onBack={() => {
              setSettingsProjectId(null);
            }}
          />
        </AgentInterface.Route>
      ) : null}
      <ProjectNudge />
    </AgentInterface>
  );
}
