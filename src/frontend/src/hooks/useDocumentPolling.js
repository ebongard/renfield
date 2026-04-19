/**
 * useDocumentPolling — minimal C1 polling loop for in-flight document uploads (#388).
 *
 * Tracks document ids that are still `pending` or `processing` and polls the
 * batch endpoint every 2 seconds to refresh their state. When a document
 * transitions to `completed` or `failed`, it's removed from the active set
 * and `onResolved` is invoked so the caller can refresh totals or fire a
 * completion notification.
 *
 * C2 will upgrade this with exponential backoff, the Page Visibility API,
 * an AbortController per fetch, and localStorage persistence of in-flight
 * uploads across page reloads. This file deliberately keeps it dumb:
 * fixed 2 s interval, no backoff, no persistence. Enough to make the
 * cutover UX honest (status moves from pending → processing → completed
 * without a manual refresh) without landing a huge surface area in one PR.
 */
import { useCallback, useEffect, useRef, useState } from 'react';
import apiClient from '../utils/axios';

const DEFAULT_POLL_INTERVAL_MS = 2000;
const TERMINAL_STATES = new Set(['completed', 'failed']);

export function useDocumentPolling({ onResolved, intervalMs = DEFAULT_POLL_INTERVAL_MS } = {}) {
  // Map<documentId, DocumentResponse>
  const [activeDocs, setActiveDocs] = useState({});
  const intervalRef = useRef(null);
  const onResolvedRef = useRef(onResolved);
  // Mirror activeDocs into a ref so poll() reads the current set even
  // when the effect doesn't restart (e.g. resolve-A + track-B land in
  // the same React batch so ids.length stays constant).
  const activeDocsRef = useRef(activeDocs);

  // Keep the callback ref fresh so parent re-renders don't restart the poll.
  useEffect(() => {
    onResolvedRef.current = onResolved;
  }, [onResolved]);

  useEffect(() => {
    activeDocsRef.current = activeDocs;
  }, [activeDocs]);

  const track = useCallback((doc) => {
    if (!doc || TERMINAL_STATES.has(doc.status)) return;
    setActiveDocs((prev) => ({ ...prev, [doc.id]: doc }));
  }, []);

  const forget = useCallback((id) => {
    setActiveDocs((prev) => {
      if (!(id in prev)) return prev;
      const next = { ...prev };
      delete next[id];
      return next;
    });
  }, []);

  const ids = Object.keys(activeDocs).map(Number);

  useEffect(() => {
    if (ids.length === 0) {
      if (intervalRef.current) {
        clearInterval(intervalRef.current);
        intervalRef.current = null;
      }
      return;
    }
    if (intervalRef.current) return; // already running

    const poll = async () => {
      const currentIds = Object.keys(activeDocsRef.current).map(Number);
      if (currentIds.length === 0) return;
      try {
        const response = await apiClient.get('/api/knowledge/documents/batch', {
          params: { ids: currentIds.join(',') },
        });
        const rows = response.data || [];
        // Compute resolved rows up front — setActiveDocs(fn) defers the
        // updater to commit time, so collecting from inside it would
        // read empty when we fire callbacks below. Derive the list from
        // the batch response, which is the single source of truth for
        // this tick.
        const resolvedThisTick = rows.filter((row) =>
          TERMINAL_STATES.has(row.status),
        );
        const seen = new Set(rows.map((row) => row.id));
        setActiveDocs((prev) => {
          const next = { ...prev };
          for (const row of rows) {
            if (TERMINAL_STATES.has(row.status)) {
              delete next[row.id];
            } else {
              next[row.id] = row;
            }
          }
          // Any id we asked about that wasn't returned (deleted by an
          // admin mid-poll?) gets dropped so we don't poll forever.
          for (const id of currentIds) {
            if (!seen.has(id)) delete next[id];
          }
          return next;
        });
        // Fire resolution callbacks outside the updater so StrictMode's
        // double-invocation in dev doesn't deliver duplicate onResolved
        // events to the caller.
        if (resolvedThisTick.length && onResolvedRef.current) {
          for (const row of resolvedThisTick) {
            onResolvedRef.current(row);
          }
        }
      } catch (err) {
        // Keep polling through transient errors; caller sees stale data,
        // not a crash. A long outage will be visible because statuses
        // stop updating — acceptable for C1.
        console.warn('[useDocumentPolling] poll failed:', err);
      }
    };

    // Kick off immediately, then on every interval tick.
    poll();
    intervalRef.current = setInterval(poll, intervalMs);

    return () => {
      if (intervalRef.current) {
        clearInterval(intervalRef.current);
        intervalRef.current = null;
      }
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [ids.length]);

  return { activeDocs, track, forget };
}
