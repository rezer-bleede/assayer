'use client';

/**
 * Sidebar with Projects: ChatGPT/Claude-style grouping.
 *
 * "All chats" is the original behaviour (every thread, no filtering);
 * selecting a project filters the same thread list by `projectId`. New
 * chats file into the active project via `projectAwareStorage`.
 */

import { AgentInterface, useNav, useThreadList } from '@openuidev/react-ui';
import { useEffect, useMemo, useRef, useState } from 'react';
import { useProjects, type ThreadWithProject } from '@/lib/projects';
import type { Theme } from '@/lib/theme';
import { FolderIcon, GearIcon, PlusIcon, TrashIcon } from './icons';

export function ProjectsSidebar({
  onOpenSettings,
  theme,
  onThemeChange,
}: {
  onOpenSettings: (id: string) => void;
  theme: Theme;
  onThemeChange: (next: Theme) => void;
}) {
  const threads = useThreadList((s) => s.threads) as unknown as ThreadWithProject[];
  const selectedThreadId = useThreadList((s) => s.selectedThreadId);
  const selectThread = useThreadList((s) => s.selectThread);
  const deleteThread = useThreadList((s) => s.deleteThread);
  const loadThreads = useThreadList((s) => s.loadThreads);

  const projects = useProjects((s) => s.projects);
  const activeProjectId = useProjects((s) => s.activeProjectId);
  const setActive = useProjects((s) => s.setActive);
  const createProject = useProjects((s) => s.createProject);

  const nav = useNav();
  const [adding, setAdding] = useState(false);
  const [newName, setNewName] = useState('');
  const requested = useRef(false);

  useEffect(() => {
    if (requested.current) return;
    requested.current = true;
    loadThreads();
  }, [loadThreads]);

  const visible = useMemo(
    () => (activeProjectId ? threads.filter((t) => t.projectId === activeProjectId) : threads),
    [threads, activeProjectId],
  );

  const openThread = (id: string) => {
    selectThread(id);
    if (nav.path !== undefined) nav.navigate(undefined);
  };

  const addProject = async () => {
    const name = newName.trim();
    setNewName('');
    setAdding(false);
    if (!name) return;
    const project = await createProject(name);
    setActive(project.id);
    if (nav.path !== undefined) nav.navigate(undefined);
  };

  return (
    <>
      <div className="openui-agent-sidebar-actions">
        <AgentInterface.SidebarHeader />
        <div className="openui-agent-sidebar-primary-actions">
          <AgentInterface.NewChatButton />
        </div>
      </div>
      <AgentInterface.SidebarContent>
        <AgentInterface.SidebarItem
          icon={<FolderIcon style={{ opacity: activeProjectId ? 0.55 : 1, fontSize: 14 }} />}
          selected={activeProjectId === null}
          onClick={() => {
            setActive(null);
            if (nav.path !== undefined) nav.navigate(undefined);
          }}
        >
          All chats
        </AgentInterface.SidebarItem>

        <div className="flex items-center justify-between px-3 pb-1 pt-3">
          <span className="text-[11px] font-medium uppercase tracking-wider text-muted">
            Projects
          </span>
          <button
            type="button"
            aria-label="New project"
            className="rounded p-0.5 text-muted hover:bg-ink/5 hover:text-fg"
            onClick={() => setAdding(true)}
          >
            <PlusIcon style={{ fontSize: 13 }} />
          </button>
        </div>

        {adding ? (
          <div className="px-3 pb-1">
            <input
              autoFocus
              value={newName}
              placeholder="Project name — Enter to create"
              onChange={(e) => setNewName(e.target.value)}
              onKeyDown={(e) => {
                if (e.key === 'Enter') void addProject();
                if (e.key === 'Escape') {
                  setAdding(false);
                  setNewName('');
                }
              }}
              onBlur={() => {
                if (!newName.trim()) setAdding(false);
              }}
              className="w-full rounded-md border border-border bg-surface px-2 py-1.5 text-[12.5px] text-fg outline-none placeholder:text-muted/70"
            />
          </div>
        ) : null}

        {projects.length === 0 && !adding ? (
          <p className="px-3 pb-2 text-[11.5px] leading-relaxed text-muted">
            Group audits into a project: custom instructions and knowledge files the agent uses
            in every chat filed there.
          </p>
        ) : null}

        {projects.map((p) => (
          <AgentInterface.SidebarItem
            key={p.id}
            icon={<FolderIcon style={{ fontSize: 14 }} />}
            selected={activeProjectId === p.id}
            onClick={() => {
              setActive(p.id);
              if (nav.path !== undefined) nav.navigate(undefined);
            }}
            trailing={
              <span
                role="button"
                tabIndex={0}
                aria-label={`${p.name} settings`}
                onClick={(e) => {
                  e.stopPropagation();
                  onOpenSettings(p.id);
                  nav.navigate(`/project/${p.id}`);
                }}
                onKeyDown={(e) => {
                  if (e.key === 'Enter') {
                    e.stopPropagation();
                    onOpenSettings(p.id);
                    nav.navigate(`/project/${p.id}`);
                  }
                }}
                className="inline-flex rounded p-0.5 text-muted hover:text-fg"
              >
                <GearIcon style={{ fontSize: 13 }} />
              </span>
            }
          >
            {p.name}
          </AgentInterface.SidebarItem>
        ))}

        <div className="px-3 pb-1 pt-4">
          <span className="text-[11px] font-medium uppercase tracking-wider text-muted">
            {activeProjectId
              ? `Chats in ${(projects.find((p) => p.id === activeProjectId)?.name ?? 'project').slice(0, 18)}`
              : 'Chats'}
          </span>
        </div>

        {visible.map((t) => (
          <AgentInterface.SidebarItem
            key={t.id}
            selected={selectedThreadId === t.id}
            onClick={() => openThread(t.id)}
            trailing={
              <span
                role="button"
                tabIndex={0}
                aria-label={`Delete ${t.title}`}
                onClick={(e) => {
                  e.stopPropagation();
                  if (window.confirm(`Delete “${t.title}”?`)) deleteThread(t.id);
                }}
                onKeyDown={(e) => {
                  if (e.key === 'Enter') {
                    e.stopPropagation();
                    if (window.confirm(`Delete ${t.title}?`)) deleteThread(t.id);
                  }
                }}
                className="inline-flex rounded p-0.5 text-muted/60 hover:text-fail"
              >
                <TrashIcon style={{ fontSize: 13 }} />
              </span>
            }
          >
            {t.title}
          </AgentInterface.SidebarItem>
        ))}

        {activeProjectId && visible.length === 0 ? (
          <p className="px-3 py-2 text-[11.5px] leading-relaxed text-muted">
            No chats here yet — while this project is selected, new chats land in it.
          </p>
        ) : null}
      </AgentInterface.SidebarContent>
      <AgentInterface.SidebarFooter>
        <AgentInterface.ThemeModeToggle mode={theme} onModeChange={onThemeChange} />
      </AgentInterface.SidebarFooter>
    </>
  );
}
