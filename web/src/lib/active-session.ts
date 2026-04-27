/**
 * Persist the *active* chat session in localStorage so that
 * mobile-app-style backgrounding, in-app navigation, and reloads
 * land back on the same TUI session instead of spawning a fresh one.
 *
 * We treat localStorage as advisory, not authoritative: the server
 * still owns session lifetime. We expire entries after `STALE_MS`
 * (currently 24h) and silently ignore anything that fails to parse.
 *
 * Multiple chat surfaces could in theory write here concurrently
 * (PWA + Safari tab on the same device). The last writer wins; the
 * session id stored is the most recently *seen* live session.
 */

const STORAGE_KEY = "hermes:active-chat-session";
const STALE_MS = 24 * 60 * 60 * 1000;

export interface ActiveChatSession {
  /** Backing TUI session id (the thing we pass as ?resume=…). */
  id: string;
  /** Wall-clock ms when we last confirmed the session was live. */
  updatedAt: number;
  /** Optional human-friendly hint for resume picker UIs. */
  title?: string;
}

export function readActiveSession(): ActiveChatSession | null {
  if (typeof window === "undefined") return null;
  try {
    const raw = window.localStorage.getItem(STORAGE_KEY);
    if (!raw) return null;
    const parsed = JSON.parse(raw) as Partial<ActiveChatSession>;
    if (!parsed?.id || typeof parsed.updatedAt !== "number") return null;
    if (Date.now() - parsed.updatedAt > STALE_MS) {
      // Expired — clear so we don't keep tripping over it.
      window.localStorage.removeItem(STORAGE_KEY);
      return null;
    }
    return {
      id: parsed.id,
      updatedAt: parsed.updatedAt,
      title: typeof parsed.title === "string" ? parsed.title : undefined,
    };
  } catch {
    return null;
  }
}

export function writeActiveSession(
  id: string,
  patch?: { title?: string },
): void {
  if (typeof window === "undefined" || !id) return;
  try {
    const prev = readActiveSession();
    const next: ActiveChatSession = {
      id,
      updatedAt: Date.now(),
      title: patch?.title ?? (prev?.id === id ? prev.title : undefined),
    };
    window.localStorage.setItem(STORAGE_KEY, JSON.stringify(next));
  } catch {
    /* private browsing, quota, etc. — non-fatal */
  }
}

export function clearActiveSession(): void {
  if (typeof window === "undefined") return;
  try {
    window.localStorage.removeItem(STORAGE_KEY);
  } catch {
    /* ignore */
  }
}

/** Format a relative-time hint for resume UIs. */
export function freshnessLabel(updatedAt: number, now = Date.now()): string {
  const delta = Math.max(0, now - updatedAt);
  const sec = Math.round(delta / 1000);
  if (sec < 30) return "just now";
  if (sec < 60) return `${sec}s ago`;
  const min = Math.round(sec / 60);
  if (min < 60) return `${min} min ago`;
  const hr = Math.round(min / 60);
  if (hr < 24) return `${hr} hr ago`;
  const days = Math.round(hr / 24);
  return `${days} day${days === 1 ? "" : "s"} ago`;
}
