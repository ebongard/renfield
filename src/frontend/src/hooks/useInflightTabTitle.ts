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

export function useInflightTabTitle(inflightCount: number, baseTitle: string): void {
  const baseRef = useRef<string>(baseTitle);
  const prevCountRef = useRef(0);
  const flashTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null);

  // Snapshot the base title on mount so subsequent re-renders that flow
  // through React state don't clobber the "clean" value we want to
  // restore to. On first visit i18next may still be hydrating when the
  // component mounts — if `baseTitle` is empty then, we fall back to
  // `document.title`. The follow-up effect below promotes the real
  // value once it lands.
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

  // i18n may resolve lazily. If the mount-time snapshot grabbed a
  // placeholder and a real title lands on a later render, promote it
  // so the `(N)` prefix rides on top of the correct base. We only
  // overwrite when the ref is empty or still equals the current page
  // title with prefixes stripped — any other value indicates the
  // caller has set a different base on purpose (route change, manual
  // document.title write).
  useEffect(() => {
    if (!baseTitle) return;
    if (baseRef.current === baseTitle) return;
    const current = baseRef.current || '';
    const currentClean = document.title
      .replace(/^\(\d+\)\s+/, '')
      .replace(/^\(✓\)\s+/, '');
    if (!current || current === currentClean) {
      baseRef.current = baseTitle;
    }
  }, [baseTitle]);

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
