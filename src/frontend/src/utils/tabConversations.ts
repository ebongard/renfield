/**
 * The conversations THIS browser tab has written into.
 *
 * A finished background scan is announced over /ws/user to every socket that may
 * see it — in a household without login that is every open tab. Only the tab the
 * request came from should interrupt its user with a notice; the others just
 * refresh quietly. The event names its conversation (`session_id`), and this
 * registry answers "did I send in that one?".
 *
 * Per tab by construction: `sessionStorage` is scoped to one tab and survives a
 * reload of it, which is exactly the lifetime of "this tab asked". Storage can be
 * unavailable (private mode, blocked site data) — then an in-memory copy still
 * covers the tab until it is reloaded.
 */

const STORAGE_KEY = 'renfield.tabConversations';
// A tab rarely writes into more than a handful of conversations; the cap only
// keeps a long-lived kiosk-style tab from growing the list forever.
const MAX_ENTRIES = 50;

let memory: string[] | null = null;

function load(): string[] {
  if (memory) return memory;
  try {
    const parsed: unknown = JSON.parse(window.sessionStorage.getItem(STORAGE_KEY) ?? '[]');
    memory = Array.isArray(parsed) ? parsed.filter((v): v is string => typeof v === 'string') : [];
  } catch {
    memory = [];
  }
  return memory;
}

export function rememberTabConversation(sessionId: string): void {
  const next = [sessionId, ...load().filter((id) => id !== sessionId)].slice(0, MAX_ENTRIES);
  memory = next;
  try {
    window.sessionStorage.setItem(STORAGE_KEY, JSON.stringify(next));
  } catch {
    /* storage unavailable: the in-memory copy still covers this tab */
  }
}

export function isTabConversation(sessionId: string): boolean {
  return load().includes(sessionId);
}

/** Test helper: forget the in-memory copy so storage is read again. */
export function resetTabConversationsForTests(): void {
  memory = null;
}
