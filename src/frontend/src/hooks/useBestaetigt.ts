import { useCallback, useEffect, useRef, useState } from 'react';

import { useConfirmObligation, useReopenObligation } from '../api/resources/brain';

/**
 * Acknowledgement ("Bestätigt") state for obligations — server-backed via the
 * obligation ledger (`POST/DELETE /api/atoms/obligations/{id}/confirm`), keyed on
 * the always-present fact `id`.
 *
 * The server `confirmed` flag (on each obligation row) is the source of truth;
 * it survives device switches and tells the deadline notifier the obligation is
 * handled. This hook layers a small optimistic override on top so the agenda can
 * demote a row instantly and offer a 5s undo without waiting on the round-trip:
 *
 *   confirm(id)  → optimistic confirmed + POST, opens the 5s undo window (`pending`)
 *   undo(id)     → optimistic un-confirm + DELETE (within the window, or via Esc)
 *   reopen(id)   → optimistic un-confirm + DELETE, no toast (not destructive)
 *   isConfirmed(id, serverConfirmed) → override if set, else the server flag
 *
 * One-time migration: any ids in the legacy per-device localStorage store are
 * POSTed to the server on first mount, then the store is reconciled away so
 * there is a single source of truth.
 */
const LEGACY_STORAGE_KEY = 'renfield.obligations.bestaetigt';
const MIGRATED_FLAG_KEY = 'renfield.obligations.bestaetigt.migrated';
export const UNDO_WINDOW_MS = 5000;

function loadLegacyIds(): number[] {
  try {
    const raw = localStorage.getItem(LEGACY_STORAGE_KEY);
    if (!raw) return [];
    const arr = JSON.parse(raw) as unknown;
    return Array.isArray(arr) ? arr.filter((x): x is number => typeof x === 'number') : [];
  } catch {
    return [];
  }
}

export interface UseBestaetigt {
  /** Override (optimistic) if set this session, else the server `confirmed` flag. */
  isConfirmed: (id: number, serverConfirmed?: boolean) => boolean;
  confirm: (id: number) => void;
  undo: (id: number) => void;
  reopen: (id: number) => void;
  /** The obligation whose undo toast is currently open, or null. */
  pending: number | null;
  /** Error message if the last confirm/reopen write failed, else null. */
  error: string | null;
}

export function useBestaetigt(): UseBestaetigt {
  const confirmMutation = useConfirmObligation();
  const reopenMutation = useReopenObligation();
  // Per-session optimistic overlay over the server `confirmed` flag.
  const [override, setOverride] = useState<Map<number, boolean>>(() => new Map());
  const [pending, setPending] = useState<number | null>(null);
  const timerRef = useRef<ReturnType<typeof setTimeout> | null>(null);

  const confirmMutate = confirmMutation.mutateAsync;
  const reopenMutate = reopenMutation.mutateAsync;

  const clearTimer = useCallback(() => {
    if (timerRef.current) {
      clearTimeout(timerRef.current);
      timerRef.current = null;
    }
  }, []);

  const setOverrideFor = useCallback((id: number, value: boolean) => {
    setOverride((prev) => new Map(prev).set(id, value));
  }, []);

  // On a failed write, drop the optimistic override so the row falls back to the
  // server `confirmed` flag (the truth — the write didn't persist). Without this
  // a failed POST/DELETE would leave the UI permanently showing the wrong state.
  const clearOverride = useCallback((id: number) => {
    setOverride((prev) => {
      if (!prev.has(id)) return prev;
      const next = new Map(prev);
      next.delete(id);
      return next;
    });
  }, []);

  const confirm = useCallback(
    (id: number) => {
      setOverrideFor(id, true);
      confirmMutate(id).catch(() => clearOverride(id));
      clearTimer();
      setPending(id);
      timerRef.current = setTimeout(() => setPending(null), UNDO_WINDOW_MS);
    },
    [confirmMutate, setOverrideFor, clearOverride, clearTimer],
  );

  const undo = useCallback(
    (id: number) => {
      setOverrideFor(id, false);
      reopenMutate(id).catch(() => clearOverride(id));
      clearTimer();
      setPending(null);
    },
    [reopenMutate, setOverrideFor, clearOverride, clearTimer],
  );

  // reopen = un-acknowledge without a toast (re-opening a stuck confirmation).
  const reopen = useCallback(
    (id: number) => {
      setOverrideFor(id, false);
      reopenMutate(id).catch(() => clearOverride(id));
    },
    [reopenMutate, setOverrideFor, clearOverride],
  );

  // Esc reverts the open undo window (D-FLOW-1 / A11Y).
  useEffect(() => {
    if (pending === null) return;
    const onKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape') undo(pending);
    };
    window.addEventListener('keydown', onKey);
    return () => window.removeEventListener('keydown', onKey);
  }, [pending, undo]);

  useEffect(() => clearTimer, [clearTimer]);

  // One-time migration of the legacy per-device localStorage store onto the
  // server ledger, then reconcile it away (single source of truth).
  useEffect(() => {
    try {
      if (localStorage.getItem(MIGRATED_FLAG_KEY)) return;
      const legacy = loadLegacyIds();
      for (const id of legacy) {
        setOverrideFor(id, true);
        confirmMutate(id).catch(() => clearOverride(id));
      }
      localStorage.removeItem(LEGACY_STORAGE_KEY);
      localStorage.setItem(MIGRATED_FLAG_KEY, '1');
    } catch {
      // localStorage unavailable (private mode) — nothing to migrate.
    }
  }, [confirmMutate, setOverrideFor]);

  const isConfirmed = useCallback(
    (id: number, serverConfirmed = false) =>
      override.has(id) ? (override.get(id) as boolean) : serverConfirmed,
    [override],
  );

  const error = confirmMutation.errorMessage ?? reopenMutation.errorMessage ?? null;

  return { isConfirmed, confirm, undo, reopen, pending, error };
}
