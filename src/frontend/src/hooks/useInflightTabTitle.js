/**
 * useInflightTabTitle — mutate document.title while uploads are in flight (#388).
 *
 * Contract:
 *   - While `inflightCount > 0`: prefix page title with `(N) `.
 *   - Transition `inflightCount > 0 → 0`: flash `(✓) ` for 30 s, then restore
 *     the bare title. If the user focuses the tab before the 30 s expire the
 *     checkmark clears immediately (the user already saw it).
 *   - Restores the bare title on unmount.
 *
 * Deliberately lives in its own hook so the title-mutation logic is
 * testable in isolation and doesn't tangle with KnowledgePage re-renders.
 */
import { useEffect, useRef } from 'react';

const FLASH_MS = 30_000;

export function useInflightTabTitle(inflightCount, baseTitle) {
  const baseRef = useRef(baseTitle);
  const prevCountRef = useRef(0);
  const flashTimerRef = useRef(null);

  // Snapshot the base title once so subsequent re-renders that flow through
  // React state don't clobber the "clean" value we want to restore to.
  useEffect(() => {
    baseRef.current = baseTitle || document.title;
    return () => {
      if (flashTimerRef.current) {
        clearTimeout(flashTimerRef.current);
        flashTimerRef.current = null;
      }
      // Restore on unmount.
      document.title = baseRef.current;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  useEffect(() => {
    const base = baseRef.current;
    if (!base) return;

    // Clear any pending flash reset — the count just changed.
    if (flashTimerRef.current) {
      clearTimeout(flashTimerRef.current);
      flashTimerRef.current = null;
    }

    if (inflightCount > 0) {
      document.title = `(${inflightCount}) ${base}`;
    } else if (prevCountRef.current > 0) {
      // Just finished — show the checkmark briefly.
      document.title = `(✓) ${base}`;
      flashTimerRef.current = setTimeout(() => {
        document.title = base;
        flashTimerRef.current = null;
      }, FLASH_MS);
    } else {
      document.title = base;
    }

    prevCountRef.current = inflightCount;
  }, [inflightCount]);

  // Auto-clear the checkmark when the user focuses the tab — by that point
  // they've already seen the status on the page itself.
  useEffect(() => {
    const onVisible = () => {
      if (
        typeof document !== 'undefined' &&
        document.visibilityState === 'visible' &&
        flashTimerRef.current
      ) {
        clearTimeout(flashTimerRef.current);
        flashTimerRef.current = null;
        document.title = baseRef.current;
      }
    };
    document.addEventListener('visibilitychange', onVisible);
    return () => {
      document.removeEventListener('visibilitychange', onVisible);
    };
  }, []);
}
