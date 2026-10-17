'use client';

import { forwardRef, useRef, useState } from 'react';
import { cn } from '@/lib/utils';

const MIN_PX = 60;
const MAX_PX = 360;

export const Composer = forwardRef<
  HTMLTextAreaElement,
  {
    onSend: (text: string) => void;
    disabled?: boolean;
    placeholder?: string;
    hint?: string | null;
  }
>(function Composer({ onSend, disabled, placeholder, hint }, ref) {
  const [value, setValue] = useState('');
  const [focused, setFocused] = useState(false);
  const taRef = useRef<HTMLTextAreaElement | null>(null);

  // merge our internal ref with the forwarded one
  const attach = (el: HTMLTextAreaElement | null) => {
    taRef.current = el;
    if (typeof ref === 'function') ref(el);
    else if (ref) ref.current = el;
  };

  const send = () => {
    const v = value.trim();
    if (!v || disabled) return;
    onSend(v);
    setValue('');
    if (taRef.current) taRef.current.style.height = `${MIN_PX}px`;
  };

  const grow = (el: HTMLTextAreaElement) => {
    el.style.height = 'auto';
    el.style.height = `${Math.min(Math.max(el.scrollHeight, MIN_PX), MAX_PX)}px`;
  };

  return (
    <div className="border-t bg-bg/85 backdrop-blur">
      <div className="mx-auto w-full max-w-5xl px-6 py-4">
        {hint && (
          <p className="mb-2 text-[11.5px] text-muted">
            <span className="mr-1.5 inline-block h-1.5 w-1.5 rounded-full bg-accent align-middle" />
            {hint}
          </p>
        )}

        <div
          className={cn(
            'surface flex items-end gap-2.5 p-2.5 transition-shadow',
            focused && 'shadow-md ring-1 ring-accent/25',
          )}
        >
          <textarea
            ref={attach}
            rows={3}
            value={value}
            disabled={disabled}
            placeholder={placeholder}
            onFocus={() => setFocused(true)}
            onBlur={() => setFocused(false)}
            onChange={(e) => {
              setValue(e.target.value);
              grow(e.target);
            }}
            onKeyDown={(e) => {
              if (e.key === 'Enter' && !e.shiftKey) {
                e.preventDefault();
                send();
              }
            }}
            className="w-full resize-none bg-transparent px-1.5 py-1 text-[13.5px] leading-relaxed outline-none placeholder:text-muted/70"
            style={{ minHeight: MIN_PX, maxHeight: MAX_PX }}
          />
          <button
            onClick={send}
            disabled={disabled || !value.trim()}
            className="btn-primary h-10 w-10 shrink-0 rounded-lg p-0"
            aria-label="Send"
          >
            <svg width="17" height="17" viewBox="0 0 16 16" fill="none">
              <path
                d="M2 8h11M9 4l4 4-4 4"
                stroke="currentColor"
                strokeWidth="1.7"
                strokeLinecap="round"
                strokeLinejoin="round"
              />
            </svg>
          </button>
        </div>

        <div className="mt-2 flex items-center text-[10.5px] text-muted/70">
          <span>
            <kbd className="rounded border px-1">Enter</kbd> send ·{' '}
            <kbd className="rounded border px-1">Shift</kbd>+<kbd className="rounded border px-1">Enter</kbd>{' '}
            newline
          </span>
          <span className="ml-auto">credentials are masked before the message is stored</span>
        </div>
      </div>
    </div>
  );
});