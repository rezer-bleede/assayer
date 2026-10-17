/**
 * Browser-side storage and id helpers that never throw.
 *
 * Two traps this avoids:
 *  1. `crypto.randomUUID()` only exists in a secure context (HTTPS or
 *     http://localhost). Opening the app on a LAN IP over plain HTTP makes it
 *     `undefined` -> TypeError -> the whole app dies before first paint.
 *  2. `localStorage` throws `SecurityError` when site data is blocked, and
 *     `QuotaExceededError` on write.
 */

function read(key: string): string | null {
  try {
    return window.localStorage.getItem(key);
  } catch {
    return null;
  }
}

function write(key: string, value: string): void {
  try {
    window.localStorage.setItem(key, value);
  } catch {
    /* storage unavailable or full — an in-memory id still works for the session */
  }
}

function clear(key: string): void {
  try {
    window.localStorage.removeItem(key);
  } catch {
    /* nothing to do */
  }
}

export const storage = { read, write, clear };

/** A random id that works in every browser context. */
export function randomId(): string {
  const c = typeof globalThis !== 'undefined' ? globalThis.crypto : undefined;
  if (c && typeof c.randomUUID === 'function') return c.randomUUID();
  if (c && typeof c.getRandomValues === 'function') {
    const bytes = c.getRandomValues(new Uint8Array(16));
    return Array.from(bytes, (b) => b.toString(16).padStart(2, '0')).join('');
  }
  // Last resort: still unique enough to key a chat session.
  return `s-${Date.now().toString(36)}-${Math.random().toString(36).slice(2, 12)}`;
}

/** Stable per-browser session id, or a fresh one when storage is unavailable. */
export function getSessionId(key: string): string {
  const existing = read(key);
  if (existing) return existing;
  const id = randomId();
  write(key, id);
  return id;
}