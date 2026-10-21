'use client';

import { useCallback, useEffect, useRef, useState } from 'react';
import { WS_URL } from './api';
import { getSessionId, storage } from './storage';
import type {
  ChatMessage,
  DataProfile,
  EtlAnalysis,
  PlanCheck,
  Question,
  RepairReport,
  RunResult,
  SourceRequirement,
} from './api';

export type AgentEvent =
  | { type: 'session.ready'; session_id: string; stage?: string }
  | { type: 'history'; messages?: ChatMessage[] }
  | { type: 'message'; id: string; role: string; content: string; created_at: string }
  | { type: 'message.start'; id: string; role: string }
  | { type: 'message.delta'; id: string; text: string }
  | { type: 'thinking'; started_at: number }
  | { type: 'thought'; text: string }
  | { type: 'stage'; stage: string }
  | { type: 'agent.question'; question?: Question } & Record<string, unknown>
  | { type: 'connection.detected'; connection_id: string; requirement_key?: string | null }
  | ({ type: 'connection.repair.suggested' } & RepairReport)
  | ({ type: 'connection.repair.exhausted' } & RepairReport)
  | {
      type: 'etl.analyzed';
      etl_id: string;
      language: string;
      origin?: string;
      repo_url?: string | null;
      files?: string[];
      sources: string[];
      targets: string[];
      transformations: { kind: string; description: string }[];
      risks: string[];
      data_profiles?: DataProfile[];
      required_sources?: SourceRequirement[];
    }
  | { type: 'source.introspected'; source_id: string; name: string; tables: number }
  | { type: 'source.error'; source_id: string; error: string }
  | { type: 'plan.ready'; run_id: string; summary: string; checks: PlanCheck[] }
  | { type: 'plan.awaiting_approval'; run_id: string }
  | { type: 'plan.building'; connections: number }
  | { type: 'run.starting'; run_id: string }
  | { type: 'run.completed'; id: string; score: RunResult['score']; results: RunResult['results'] }
  | { type: 'check.completed'; run_id: string; check_id: string; status: string; name: string }
  | { type: 'lineage.bound'; bound: string[] }
  | { type: 'report.ready'; run_id: string; report: string; score: number }
  | { type: 'done'; stage?: string }
  | { type: 'error'; message: string };

const STORAGE_KEY = 'qualitest.session';

export function useAgentChat() {
  const [sessionId, setSessionId] = useState<string | null>(null);
  const [messages, setMessages] = useState<ChatMessage[]>([]);
  const [pending, setPending] = useState(false);
  const [plan, setPlan] = useState<PlanCheck[] | null>(null);
  const [planRunId, setPlanRunId] = useState<string | null>(null);
  const [progress, setProgress] = useState<Record<string, string>>({});
  const [runId, setRunId] = useState<string | null>(null);
  const [connections, setConnections] = useState<string[]>([]);
  const [etlId, setEtlId] = useState<string | null>(null);
  const [analysis, setAnalysis] = useState<EtlAnalysis | null>(null);
  const [question, setQuestion] = useState<Question | null>(null);
  const [connectionSuggestion, setConnectionSuggestion] = useState<Record<string, unknown> | null>(
    null,
  );
  const [stage, setStage] = useState<string>('need_etl');
  const [error, setError] = useState<string | null>(null);
  const wsRef = useRef<WebSocket | null>(null);
  const [connected, setConnected] = useState(false);
  /** the assistant message currently streaming in — finalized by its `message` frame */
  const [stream, setStream] = useState<{ id: string; content: string } | null>(null);
  /** latest agent status line ("Profiling orders…", "Drafting the plan…") */
  const [thought, setThought] = useState<string | null>(null);

  const connect = useCallback((sid: string) => {
    wsRef.current?.close();
    const ws = new WebSocket(`${WS_URL}/ws/chat`);
    wsRef.current = ws;

    ws.onopen = () => {
      ws.send(JSON.stringify({ session_id: sid }));
    };

    ws.onmessage = (e) => {
      let ev: AgentEvent;
      try {
        // The API wraps every frame as {type, session_id, run_id, ts, data}.
        // Flatten `data` up so handlers can read `ev.messages` / `ev.checks`
        // directly; leaving it nested silently yields `undefined` everywhere.
        const raw = JSON.parse(e.data) as { type?: string; data?: Record<string, unknown> };
        ev = { ...(raw.data ?? {}), type: raw.type } as AgentEvent;
        if (!ev.type) return;
      } catch {
        console.debug('[qualitest] ignoring non-JSON frame');
        return;
      }

      switch (ev.type) {
        case 'session.ready':
          setConnected(true);
          if (ev.stage) setStage(ev.stage);
          break;
        case 'history':
          // guarded: a malformed frame must not blank the transcript
          setMessages(Array.isArray(ev.messages) ? ev.messages : []);
          setStream(null);
          setThought(null);
          break;
        case 'message': {
          const m: ChatMessage = {
            id: ev.id,
            role: ev.role as ChatMessage['role'],
            content: ev.content,
            created_at: ev.created_at,
            attachments: [],
          };
          setMessages((prev) => {
            // a frame matching an in-flight stream finalizes it in place;
            // unknown ids append as before (replayed history, older servers)
            const idx = prev.findIndex((x) => x.id === m.id);
            if (idx < 0) return [...prev, m];
            const next = [...prev];
            next[idx] = m;
            return next;
          });
          setStream((s) => (s && s.id === m.id ? null : s));
          setThought(null);
          break;
        }
        case 'message.start':
          setStream({ id: ev.id, content: '' });
          setThought(null);
          break;
        case 'message.delta':
          // deltas arrive batched (the server pumps every 50ms), so frequent
          // state updates here are already coalesced upstream
          setStream((s) =>
            s && s.id === ev.id ? { id: s.id, content: s.content + (ev.text ?? '') } : s,
          );
          break;
        case 'thinking':
          setPending(true);
          setThought(null);
          break;
        case 'thought':
          setThought(ev.text || null);
          break;
        case 'stage':
          setStage(ev.stage);
          break;
        case 'agent.question': {
          // the frame's `data` IS the question (title/prompt/options at the root)
          const q = ev as unknown as Question;
          setQuestion(q?.title ? q : null);
          if (q?.stage) setStage(q.stage);
          break;
        }
        case 'connection.detected':
          setConnections((c) => [...new Set([...c, ev.connection_id])]);
          setConnectionSuggestion(null);
          // answering a source question resolves it; the agent asks for the next
          if (ev.requirement_key) setQuestion((q) => (q?.requirement_key === ev.requirement_key ? null : q));
          break;
        case 'connection.repair.suggested':
          setConnectionSuggestion(ev.suggested ?? null);
          break;
        case 'connection.repair.exhausted':
          setConnectionSuggestion(null);
          break;
        case 'etl.analyzed':
          setEtlId(ev.etl_id);
          setAnalysis({
            id: ev.etl_id,
            language: ev.language,
            label: null,
            origin: ev.origin ?? 'unknown',
            repo_url: ev.repo_url ?? null,
            files: ev.files ?? [],
            sources: ev.sources ?? [],
            targets: ev.targets ?? [],
            transformations: ev.transformations ?? [],
            risks: ev.risks ?? [],
            data_profiles: ev.data_profiles ?? [],
            required_sources: ev.required_sources ?? [],
          });
          break;
        case 'plan.building':
          setQuestion(null);
          break;
        case 'plan.ready':
          setPlan(ev.checks);
          setPlanRunId(ev.run_id);
          setQuestion(null);
          setProgress({});
          break;
        case 'plan.awaiting_approval':
          setPending(false);
          break;
        case 'run.starting':
          setRunId(ev.run_id);
          setProgress({});
          break;
        case 'check.completed':
          setProgress((p) => ({ ...p, [ev.check_id]: ev.status }));
          break;
        case 'run.completed':
          setProgress({});
          setPending(false);
          setStage('done');
          break;
        case 'error':
          setError(ev.message);
          setPending(false);
          setStream(null);
          setThought(null);
          break;
        case 'done':
          setPending(false);
          setStream(null);
          setThought(null);
          if (ev.stage) setStage(ev.stage);
          break;
      }
    };

    ws.onerror = () => setError(`Cannot reach the agent at ${WS_URL}. Is the api service up?`);
    ws.onclose = () => setConnected(false);
  }, []);

  useEffect(() => {
    // Must never throw: this runs before first paint, and an uncaught error here
    // takes down the whole app. `getSessionId` falls back safely when
    // crypto.randomUUID or localStorage are unavailable (insecure origin,
    // blocked site data).
    const sid = getSessionId(STORAGE_KEY);
    setSessionId(sid);
    connect(sid);
    return () => wsRef.current?.close();
  }, [connect]);

  const sendRaw = useCallback((body: Record<string, unknown>) => {
    const ws = wsRef.current;
    if (!ws || ws.readyState !== WebSocket.OPEN) return false;
    ws.send(JSON.stringify(body));
    return true;
  }, []);

  const send = useCallback(
    (content: string, action?: 'approve') => {
      const ok = sendRaw({ content, action });
      if (ok && action === 'approve') {
        setPlan(null);
        setPending(true);
      }
      return ok;
    },
    [sendRaw],
  );

  /**
   * Notify the agent about something already handed to REST (a cloned repo,
   * an uploaded codebase, a verified connection). Credentials never ride here.
   */
  const sendAction = useCallback(
    (action: string, payload: Record<string, unknown>, content = '') =>
      sendRaw({ content, action, payload }),
    [sendRaw],
  );

  const reset = useCallback(() => {
    storage.clear(STORAGE_KEY);
    window.location.reload();
  }, []);

  return {
    sessionId,
    messages,
    pending,
    stream,
    thought,
    plan,
    planRunId,
    progress,
    runId,
    connections,
    etlId,
    analysis,
    question,
    connectionSuggestion,
    stage,
    error,
    send,
    sendAction,
    reset,
    connected,
  };
}
