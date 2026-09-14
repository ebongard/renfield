import { useEffect, useRef } from 'react';

import { SCAN_JOB_FINISHED_EVENT, type ScanJobFinishedDetail } from './useUserEvents';

/**
 * Reload the open conversation when a background scan finished INTO it.
 *
 * The scan's outcome is appended server-side to the conversation that requested
 * it. Reloading pulls it in without a manual refresh — but never underneath a
 * streaming turn, which a reload would clobber: an event that arrives mid-turn
 * is remembered and the reload runs once, as soon as the turn ends.
 *
 * An event naming a DIFFERENT conversation leaves this one alone — in a household
 * without login every tab receives every scan's event. An event without a
 * conversation (older backend) reloads, as before.
 *
 * `loading`, `reload` and `sessionId` are read through refs, so the window
 * listener is registered once rather than on every turn start/end.
 */
export function useReloadOnScanJobFinished(
  loading: boolean,
  reload: () => Promise<void>,
  sessionId: string | null,
): void {
  const pendingRef = useRef(false);
  const loadingRef = useRef(loading);
  const reloadRef = useRef(reload);
  const sessionIdRef = useRef(sessionId);
  loadingRef.current = loading;
  reloadRef.current = reload;
  sessionIdRef.current = sessionId;

  useEffect(() => {
    const onFinished = (event: Event) => {
      const target = (event as CustomEvent<ScanJobFinishedDetail>).detail?.sessionId;
      if (target && target !== sessionIdRef.current) return;
      if (loadingRef.current) {
        pendingRef.current = true;
        return;
      }
      void reloadRef.current();
    };
    window.addEventListener(SCAN_JOB_FINISHED_EVENT, onFinished);
    return () => window.removeEventListener(SCAN_JOB_FINISHED_EVENT, onFinished);
  }, []);

  useEffect(() => {
    if (!loading && pendingRef.current) {
      pendingRef.current = false;
      void reloadRef.current();
    }
  }, [loading]);
}

export default useReloadOnScanJobFinished;
