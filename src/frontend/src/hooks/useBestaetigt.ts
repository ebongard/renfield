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
}

export function useBestaetigt(): UseBestaetigt {
  const confirmMutation = useConfirmObligation();
  const reopenMutation = useReopenObligation();
  // Per-session optimistic overlay over the server `confirmed` flag.
  const [override, setOverride] = useState<Map<number, boolean>>(() => new Map());
  const [pending, setPending] = useState<number | null>(null);
  const timerRef = useRef<ReturnType<typeof setTimeout> | null>(null);

  const confirmMutate = confirmMutation.mutate;
  const reopenMutate = reopenMutation.mutate;

  const clearTimer = useCallback(() => {
    if (timerRef.current) {
      clearTimeout(timerRef.current);
      timerRef.current = null;
    }
  }, []);

  const setOverrideFor = useCallback((id: number, value: boolean) => {
    setOverride((prev) => new Map(prev).set(id, value));
  }, []);

  const confirm = useCallback(
    (id: number) => {
      setOverrideFor(id, true);
      confirmMutate(id);
      clearTimer();
      setPending(id);
      timerRef.current = setTimeout(() => setPending(null), UNDO_WINDOW_MS);
    },
    [confirmMutate, setOverrideFor, clearTimer],
  );

  const undo = useCallback(
    (id: number) => {
      setOverrideFor(id, false);
      reopenMutate(id);
      clearTimer();
      setPending(null);
    },
    [reopenMutate, setOverrideFor, clearTimer],
  );

  // reopen = un-acknowledge without a toast (re-opening a stuck confirmation).
  const reopen = useCallback(
    (id: number) => {
      setOverrideFor(id, false);
      reopenMutate(id);
    },
    [reopenMutate, setOverrideFor],
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
        confirmMutate(id);
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

  return { isConfirmed, confirm, undo, reopen, pending };
}
