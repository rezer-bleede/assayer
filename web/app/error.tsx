'use client';

import { useEffect } from 'react';

/**
 * Route-level error boundary.
 *
 * Without this, any uncaught render error shows Next.js's opaque
 * "Application error: a client-side exception has occurred" page. This surfaces
 * the actual message and keeps the chat recoverable with one click.
 */
export default function Error({
  error,
  reset,
}: {
  error: Error & { digest?: string };
  reset: () => void;
}) {
  useEffect(() => {
    console.error('[qualitest] unhandled client error:', error);
  }, [error]);

  return (
    <div className="flex min-h-dvh items-center justify-center px-6">
      <div className="surface w-full max-w-lg p-6">
        <h1 className="text-base font-semibold">Something broke in the chat window</h1>
        <p className="mt-1 text-[13px] text-muted">
          The agent backend may still be running — the UI hit an error while rendering.
        </p>

        <pre className="mt-4 max-h-40 overflow-auto rounded-lg bg-ink/[0.04] p-3 font-mono text-[11.5px] leading-relaxed dark:bg-white/[0.06]">
          {error.message || 'Unknown error'}
          {error.digest ? `\n\ndigest: ${error.digest}` : ''}
        </pre>

        <div className="mt-4 flex flex-wrap gap-2">
          <button className="btn-primary" onClick={reset}>
            Try again
          </button>
          <button className="btn-ghost" onClick={() => window.location.reload()}>
            Reload
          </button>
          <a className="btn-ghost" href={process.env.NEXT_PUBLIC_API_URL ?? 'http://localhost:8010'}>
            API health
          </a>
        </div>
      </div>
    </div>
  );
}