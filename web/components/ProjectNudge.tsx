'use client';

/**
 * One-time prompt on the welcome screen to pick a project for the new chat.
 * Projects are optional, but recommended — so the app asks once per browser
 * session and never nags after a choice or dismissal.
 */

import { useNav, useThreadList } from '@openuidev/react-ui';
import { useState } from 'react';
import { useProjects } from '@/lib/projects';

const DECIDED_KEY = 'assayer.project-nudge-decided';

function decided(): boolean {
  try {
    return window.sessionStorage.getItem(DECIDED_KEY) === '1';
  } catch {
    return false;
  }
}

export function ProjectNudge() {
  const nav = useNav();
  const selectedThreadId = useThreadList((s) => s.selectedThreadId);
  const projects = useProjects((s) => s.projects);
  const loaded = useProjects((s) => s.loaded);
  const activeProjectId = useProjects((s) => s.activeProjectId);
  const setActive = useProjects((s) => s.setActive);
  const createProject = useProjects((s) => s.createProject);

  const [dismissed, setDismissed] = useState(decided);
  const [adding, setAdding] = useState(false);
  const [name, setName] = useState('');

  const freshChat = selectedThreadId === null && nav.path === undefined;
  if (dismissed || !loaded || activeProjectId !== null || !freshChat) return null;

  const decide = (id: string | null) => {
    try {
      window.sessionStorage.setItem(DECIDED_KEY, '1');
    } catch {
      /* private mode: state alone is enough */
    }
    setActive(id);
    setDismissed(true);
  };

  const add = async () => {
    const trimmed = name.trim();
    setAdding(false);
    setName('');
    if (!trimmed) return;
    const project = await createProject(trimmed);
    decide(project.id);
  };

  return (
    <div className="pointer-events-none fixed inset-x-0 bottom-28 z-40 flex justify-center px-4">
      <div className="pointer-events-auto w-full max-w-md rounded-2xl border border-border bg-surface/95 p-4 shadow-lg backdrop-blur">
        <p className="mb-3 text-[13px] leading-relaxed text-fg">
          {projects.length > 0
            ? 'Run this audit inside a project? Its instructions and knowledge files apply to every chat filed there.'
            : 'Group audits into a project — custom instructions and knowledge files the agent uses in every chat inside it.'}
        </p>
        <div className="flex flex-wrap items-center gap-2">
          {projects.map((p) => (
            <button
              key={p.id}
              type="button"
              onClick={() => decide(p.id)}
              className="rounded-full border border-border px-3 py-1.5 text-[12.5px] text-fg hover:border-accent hover:text-accent"
            >
              {p.name}
            </button>
          ))}
          {adding ? (
            <input
              autoFocus
              value={name}
              placeholder="Project name"
              onChange={(e) => setName(e.target.value)}
              onKeyDown={(e) => {
                if (e.key === 'Enter') void add();
                if (e.key === 'Escape') {
                  setAdding(false);
                  setName('');
                }
              }}
              onBlur={() => {
                if (!name.trim()) setAdding(false);
                else void add();
              }}
              className="w-40 rounded-full border border-accent px-3 py-1.5 text-[12.5px] text-fg outline-none placeholder:text-muted/70"
            />
          ) : (
            <button
              type="button"
              onClick={() => setAdding(true)}
              className="rounded-full border border-dashed border-border px-3 py-1.5 text-[12.5px] text-muted hover:border-accent hover:text-accent"
            >
              + New project
            </button>
          )}
          <button
            type="button"
            onClick={() => decide(null)}
            className="ml-auto text-[12px] text-muted underline-offset-2 hover:text-fg hover:underline"
          >
            No, keep chats unfiled
          </button>
        </div>
      </div>
    </div>
  );
}
