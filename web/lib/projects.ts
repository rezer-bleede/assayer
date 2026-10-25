'use client';

/**
 * Projects: ChatGPT/Claude-style grouping of chats.
 *
 * The store owns the project list and which project new chats file into.
 * `projectAwareStorage` wraps the library's restStorage so a chat created
 * while a project is active is bound to it server-side from its first
 * message — unfiled chats behave exactly as before.
 */

import { restStorage, type ChatStorage } from '@openuidev/react-ui';
import { create } from 'zustand';
import { API_URL, api, type Project } from './api';
import { storage } from './storage';

const ACTIVE_KEY = 'assayer.active-project';

type ProjectsState = {
  projects: Project[];
  loaded: boolean;
  activeProjectId: string | null;
  load: () => Promise<void>;
  setActive: (id: string | null) => void;
  createProject: (name: string, instructions?: string) => Promise<Project>;
  updateProject: (id: string, patch: { name?: string; instructions?: string }) => Promise<void>;
  removeProject: (id: string) => Promise<void>;
};

function storedActiveId(): string | null {
  return storage.read(ACTIVE_KEY);
}

export const useProjects = create<ProjectsState>((set, get) => ({
  projects: [],
  loaded: false,
  activeProjectId: typeof window !== 'undefined' ? storedActiveId() : null,

  load: async () => {
    try {
      const { projects } = await api.projects();
      set((s) => {
        const known = new Set(projects.map((p) => p.id));
        const active =
          s.activeProjectId && known.has(s.activeProjectId) ? s.activeProjectId : null;
        return { projects, loaded: true, activeProjectId: active };
      });
    } catch {
      set({ loaded: true });
    }
  },

  setActive: (id) => {
    if (id) storage.write(ACTIVE_KEY, id);
    else storage.clear(ACTIVE_KEY);
    set({ activeProjectId: id });
  },

  createProject: async (name, instructions = '') => {
    const project = await api.createProject(name, instructions);
    set((s) => ({ projects: [project, ...s.projects] }));
    return project;
  },

  updateProject: async (id, patch) => {
    const updated = await api.updateProject(id, patch);
    set((s) => ({
      projects: s.projects.map((p) =>
        p.id === id ? { ...p, ...updated, threadCount: p.threadCount, fileCount: p.fileCount } : p,
      ),
    }));
  },

  removeProject: async (id) => {
    await api.deleteProject(id);
    set((s) => ({
      projects: s.projects.filter((p) => p.id !== id),
      activeProjectId: s.activeProjectId === id ? null : s.activeProjectId,
    }));
    get().load();
  },
}));

export function projectAwareStorage(): ChatStorage {
  const withSession: typeof fetch = (input, init) => fetch(input, { ...init, credentials: 'include' });
  const base = restStorage({ baseUrl: `${API_URL}/api/threads`, fetch: withSession });
  return {
    ...base,
    thread: {
      ...base.thread,
      createThread: async (firstMessage) => {
        const projectId = useProjects.getState().activeProjectId;
        const res = await fetch(`${API_URL}/api/threads/create`, {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          credentials: 'include',
          cache: 'no-store',
          body: JSON.stringify({ messages: [firstMessage], projectId }),
        });
        if (!res.ok) throw new Error(`createThread failed: ${res.status} ${res.statusText}`);
        return res.json();
      },
    },
  };
}

/** Extra fields (projectId) ride along on the library's Thread objects. */
export type ThreadWithProject = {
  id: string;
  title: string;
  createdAt: string | number;
  isPending?: boolean;
  projectId?: string | null;
};
