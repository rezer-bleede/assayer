'use client';

/**
 * Project settings: name, custom instructions, and knowledge files.
 * Rendered inside an `AgentInterface.Route` — it replaces the thread area.
 */

import { useNav, useThreadList } from '@openuidev/react-ui';
import { useCallback, useEffect, useState } from 'react';
import { api, type ProjectDetail } from '@/lib/api';
import { useProjects } from '@/lib/projects';
import { BackIcon, FileIcon, TrashIcon } from './icons';

const FILE_ACCEPT = '.md,.markdown,.txt,.sql,.ddl,.csv,.tsv,.json,.jsonl,.ndjson,.yml,.yaml,.toml,.ini,.cfg,.proto,.py,.pyi';

function formatSize(bytes: number): string {
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
  return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
}

export function ProjectSettings({ projectId, onBack }: { projectId: string; onBack: () => void }) {
  const nav = useNav();
  const selectThread = useThreadList((s) => s.selectThread);
  const updateProject = useProjects((s) => s.updateProject);
  const removeProject = useProjects((s) => s.removeProject);
  const setActive = useProjects((s) => s.setActive);
  const activeProjectId = useProjects((s) => s.activeProjectId);

  const [detail, setDetail] = useState<ProjectDetail | null>(null);
  const [name, setName] = useState('');
  const [instructions, setInstructions] = useState('');
  const [savedAt, setSavedAt] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  const refresh = useCallback(async () => {
    try {
      const d = await api.projectDetail(projectId);
      setDetail(d);
      setName(d.name);
      setInstructions(d.instructions);
    } catch (e) {
      setNotice((e as Error).message);
    }
  }, [projectId]);

  useEffect(() => {
    void refresh();
  }, [refresh]);

  const persist = async (patch: { name?: string; instructions?: string }) => {
    setBusy(true);
    try {
      await updateProject(projectId, patch);
      setSavedAt(new Date().toLocaleTimeString());
      setNotice(null);
      await refresh();
    } catch (e) {
      setNotice((e as Error).message);
    } finally {
      setBusy(false);
    }
  };

  const upload = async (list: FileList | null) => {
    if (!list || list.length === 0) return;
    setBusy(true);
    try {
      const res = await api.uploadProjectFiles(projectId, Array.from(list));
      setNotice(
        res.rejected.length
          ? `Rejected: ${res.rejected.map((r) => `${r.name} (${r.reason})`).join(', ')}`
          : null,
      );
      await refresh();
    } catch (e) {
      setNotice((e as Error).message);
    } finally {
      setBusy(false);
    }
  };

  const backToThread = () => {
    onBack();
    nav.navigate(undefined);
  };

  if (!detail) {
    return (
      <div className="flex h-full items-center justify-center text-sm text-muted">
        {notice ?? 'Loading project…'}
      </div>
    );
  }

  return (
    <div className="h-full overflow-auto">
      <div className="mx-auto w-full max-w-2xl px-6 py-8">
        <button
          type="button"
          onClick={backToThread}
          className="mb-6 inline-flex items-center gap-1.5 text-[13px] text-muted hover:text-fg"
        >
          <BackIcon style={{ fontSize: 14 }} /> Back to chats
        </button>

        <h1 className="mb-1 text-xl font-medium text-fg">Project settings</h1>
        <p className="mb-6 text-[13px] leading-relaxed text-muted">
          Instructions and knowledge files travel with every chat in this project — the agent
          reads them when it plans, tests and reports.
        </p>

        <label className="mb-1 block text-[11px] font-medium uppercase tracking-wider text-muted">
          Name
        </label>
        <input
          value={name}
          onChange={(e) => setName(e.target.value)}
          onBlur={() => {
            if (name.trim() && name.trim() !== detail.name) void persist({ name: name.trim() });
            else setName(detail.name);
          }}
          onKeyDown={(e) => {
            if (e.key === 'Enter') (e.target as HTMLInputElement).blur();
          }}
          className="mb-6 w-full rounded-lg border border-border bg-surface px-3 py-2 text-sm text-fg outline-none focus:border-accent"
        />

        <label className="mb-1 block text-[11px] font-medium uppercase tracking-wider text-muted">
          Custom instructions
        </label>
        <textarea
          value={instructions}
          rows={5}
          placeholder="e.g. Treat staging failures as warnings; verify checkout totals against the business rules; prioritize critical user journeys."
          onChange={(e) => setInstructions(e.target.value)}
          onBlur={() => {
            if (instructions !== detail.instructions) void persist({ instructions });
          }}
          className="w-full resize-y rounded-lg border border-border bg-surface px-3 py-2 text-sm leading-relaxed text-fg outline-none focus:border-accent"
        />
        <div className="mb-6 mt-1 flex items-center gap-2 text-[11.5px] text-muted">
          {busy ? <span>saving…</span> : null}
          {!busy && savedAt ? <span>saved at {savedAt}</span> : null}
        </div>

        <label className="mb-1 block text-[11px] font-medium uppercase tracking-wider text-muted">
          Knowledge files
        </label>
        <div className="mb-2 flex flex-col gap-1">
          {(detail.files ?? []).map((f) => (
            <div
              key={f.id}
              className="flex items-center gap-2 rounded-md border border-border bg-surface px-3 py-2 text-[13px] text-fg"
            >
              <FileIcon style={{ fontSize: 14 }} className="text-muted" />
              <span className="min-w-0 flex-1 truncate">{f.name}</span>
              <span className="text-[11px] text-muted">{formatSize(f.size)}</span>
              <button
                type="button"
                aria-label={`Delete ${f.name}`}
                className="rounded p-1 text-muted hover:text-fail"
                onClick={async () => {
                  setBusy(true);
                  try {
                    await api.deleteProjectFile(projectId, f.id);
                    await refresh();
                  } finally {
                    setBusy(false);
                  }
                }}
              >
                <TrashIcon style={{ fontSize: 13 }} />
              </button>
            </div>
          ))}
          {(detail.files ?? []).length === 0 ? (
            <p className="text-[12.5px] text-muted">
              No files yet — add business rules, test expectations, runbooks or technical docs
              (text files, ≤ 256 KB each).
            </p>
          ) : null}
        </div>
        <label className="mb-8 inline-flex cursor-pointer items-center gap-2 rounded-lg border border-border px-3 py-2 text-[13px] text-fg hover:bg-ink/5">
          <FileIcon style={{ fontSize: 14 }} className="text-muted" />
          Add files
          <input
            type="file"
            multiple
            accept={FILE_ACCEPT}
            className="hidden"
            onChange={(e) => {
              void upload(e.target.files);
              e.target.value = '';
            }}
          />
        </label>
        {notice ? (
          <p className="-mt-6 mb-6 text-[12px] leading-relaxed text-warn">{notice}</p>
        ) : null}

        <label className="mb-1 block text-[11px] font-medium uppercase tracking-wider text-muted">
          Chats in this project
        </label>
        <div className="mb-10 flex flex-col gap-1">
          {(detail.threads ?? []).map((t) => (
            <button
              key={t.id}
              type="button"
              onClick={() => {
                selectThread(t.id);
                backToThread();
              }}
              className="rounded-md border border-border bg-surface px-3 py-2 text-left text-[13px] text-fg hover:bg-ink/5"
            >
              {t.title}
            </button>
          ))}
          {(detail.threads ?? []).length === 0 ? (
            <p className="text-[12.5px] text-muted">
              None yet. Select this project in the sidebar, then start a new chat.
            </p>
          ) : null}
        </div>

        <button
          type="button"
          className="rounded-lg border border-fail/40 px-3 py-2 text-[13px] text-fail hover:bg-fail/10"
          onClick={async () => {
            if (
              !window.confirm(
                `Delete project “${detail.name}”? Its chats are kept but become unfiled.`,
              )
            )
              return;
            if (activeProjectId === projectId) setActive(null);
            await removeProject(projectId);
            backToThread();
          }}
        >
          Delete project
        </button>
      </div>
    </div>
  );
}
