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
import { qualitestLibrary } from '@/lib/openui/library';
import { projectAwareStorage, useProjects } from '@/lib/projects';
import { useTheme } from '@/lib/theme';
import { ProjectsSidebar } from '@/components/ProjectsSidebar';
import { ProjectSettings } from '@/components/ProjectSettings';
import { ProjectNudge } from '@/components/ProjectNudge';

const llm = fetchLLM({
  url: `${API_URL}/agent`,
  streamAdapter: agUIAdapter(),
});

const threadStorage = projectAwareStorage();

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

export default function AgentShell() {
  const { theme, setChoice } = useTheme();
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
      componentLibrary={qualitestLibrary}
      agentName="qualitest"
      starters={starters}
      theme={{ mode: theme }}
      path={navPath}
      onNavigate={setNavPath}
    >
      <AgentInterface.Sidebar>
        <ProjectsSidebar
          theme={theme}
          onThemeChange={(next) => setChoice(next)}
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
