import { useEffect, useRef } from 'react';

import { SCAN_JOB_FINISHED_EVENT } from './useUserEvents';

/**
 * Reload the open conversation when a background scan finished.
 *
 * The scan's outcome is appended server-side to the conversation that requested
 * it. Reloading pulls it in without a manual refresh — but never underneath a
 * streaming turn, which a reload would clobber: an event that arrives mid-turn
 * is remembered and the reload runs once, as soon as the turn ends.
 *
 * `loading` and `reload` are read through refs, so the window listener is
 * registered once rather than on every turn start/end.
 */
export function useReloadOnScanJobFinished(loading: boolean, reload: () => Promise<void>): void {
  const pendingRef = useRef(false);
  const loadingRef = useRef(loading);
  const reloadRef = useRef(reload);
  loadingRef.current = loading;
  reloadRef.current = reload;

  useEffect(() => {
    const onFinished = () => {
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
