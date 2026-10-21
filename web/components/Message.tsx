'use client';

import { useEffect, useRef, useState } from 'react';
import type { ChatMessage } from '@/lib/api';
import { cn } from '@/lib/utils';

function Markdown({ text }: { text: string }) {
  const blocks: React.ReactNode[] = [];
  const parts = text.split(/```/);
  parts.forEach((part, i) => {
    if (i % 2 === 1) {
      blocks.push(
        <pre key={i} className="whitespace-pre-wrap">
          {part.trim()}
        </pre>,
      );
      return;
    }
    const lines = part.split('\n');
    let list: React.ReactNode[] = [];
    let buf: React.ReactNode[] = [];

    const flush = () => {
      if (buf.length) {
        blocks.push(
          <p key={`p${blocks.length}`} className="my-1.5">
            {buf}
          </p>,
        );
        buf = [];
      }
    };

    for (const line of lines) {
      const md = inline(line);
      if (/^\s*[-*]\s+/.test(line)) {
        if (buf.length) flush();
        list.push(
          <li key={list.length} className="ml-4 list-disc marker:text-muted/60">
            {inline(line.replace(/^\s*[-*]\s+/, ''))}
          </li>,
        );
      } else if (/^\s*\d+\.\s+/.test(line)) {
        if (buf.length) flush();
        list.push(
          <li key={list.length} className="ml-4 list-decimal marker:text-muted/60">
            {inline(line.replace(/^\s*\d+\.\s+/, ''))}
          </li>,
        );
      } else if (line.trim() === '') {
        flush();
      } else {
        buf.push(md);
      }
    }
    flush();
    if (list.length) {
      blocks.push(
        <ul key={`ul${blocks.length}`} className="my-1.5 space-y-0.5">
          {list}
        </ul>,
      );
    }
  });

  return <div className="prose-mini">{blocks}</div>;

  function inline(s: string): React.ReactNode {
    const parts2 = s.split(/(\*\*[^*]+\*\*|`[^`]+`)/g);
    return parts2.map((p, j) => {
      if (p.startsWith('**') && p.endsWith('**'))
        return (
          <strong key={j} className="font-semibold">
            {p.slice(2, -2)}
          </strong>
        );
      if (p.startsWith('`') && p.endsWith('`'))
        return (
          <code key={j} className="rounded bg-ink/5 px-1 py-0.5 font-mono text-[12px] dark:bg-white/10">
            {p.slice(1, -1)}
          </code>
        );
      return <span key={j}>{p}</span>;
    });
  }
}

export function MessageBubble({
  message,
  streaming,
}: {
  message: ChatMessage;
  streaming?: boolean;
}) {
  const isUser = message.role === 'user';
  return (
    <div className={cn('flex w-full animate-fade-up', isUser ? 'justify-end' : 'justify-start')}>
      <div
        className={cn(
          'max-w-[min(62rem,92%)] whitespace-pre-wrap rounded-2xl px-4 py-2.5 text-[13.5px] leading-relaxed',
          isUser
            ? 'rounded-br-sm bg-accent text-white'
            : 'surface rounded-bl-sm text-fg',
        )}
      >
        {isUser ? (
          message.content
        ) : (
          <>
            <Markdown text={message.content} />
            {streaming && (
              <span
                aria-hidden
                className="ml-0.5 inline-block h-3.5 w-[6px] animate-pulse rounded-[1px] bg-accent align-middle"
              />
            )}
          </>
        )}
      </div>
    </div>
  );
}

/** Pending-agent bubble: bouncing dots plus the agent's live status line. */
export function ThinkingStatus({ thought }: { thought?: string | null }) {
  return (
    <div className="flex justify-start" aria-live="polite" aria-label="Agent working">
      <div className="surface flex items-center gap-2.5 rounded-2xl rounded-bl-sm px-4 py-3">
        <span className="flex items-center gap-1">
          {[0, 1, 2].map((i) => (
            <span
              key={i}
              className="h-1.5 w-1.5 animate-bounce rounded-full bg-muted/70"
              style={{ animationDelay: `${i * 140}ms`, animationDuration: '900ms' }}
            />
          ))}
        </span>
        <span className="min-w-0 truncate text-[12px] text-muted">
          <span className="animate-pulse">{thought ?? 'Thinking…'}</span>
        </span>
      </div>
    </div>
  );
}

export function useAutoScroll(dep: unknown) {
  const ref = useRef<HTMLDivElement>(null);
  const [stick, setStick] = useState(true);
  useEffect(() => {
    const el = ref.current;
    if (!el || !stick) return;
    el.scrollTo({ top: el.scrollHeight, behavior: 'smooth' });
  }, [dep, stick]);
  const onScroll = () => {
    const el = ref.current;
    if (!el) return;
    setStick(el.scrollHeight - el.scrollTop - el.clientHeight < 120);
  };
  return { ref, onScroll };
}