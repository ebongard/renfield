/**
 * useDocumentPolling — C2 polling loop for in-flight document uploads (#388).
 *
 * Tracks document ids that are still `pending` or `processing` and polls the
 * batch endpoint until they reach a terminal state.
 *
 * C2 additions over C1:
 *   - Exponential backoff 1 → 2 → 4 → 8 → 10 s (reset on any status/stage/
 *     page change — user just got useful info, keep it snappy).
 *   - Page Visibility API: tab hidden → pause interval. Tab visible again →
 *     single catch-up fetch, then resume.
 *   - AbortController on every fetch; component unmount aborts in-flight.
 *   - 30-min per-document timeout. After the cap, the entry is dropped
 *     locally (the DB row is left untouched) and `onTimeout(row)` fires so
 *     the page can render a Retry CTA.
 *   - localStorage persistence at key `renfield.kb.inflight`. Every `track()`
 *     call persists the entry; terminal or >10 min stale entries are trimmed.
 *     On mount we hydrate any still-pending entries so the spinner survives
 *     a page reload.
 *
 * Callbacks never run inside `setActiveDocs`'s updater function — StrictMode
 * would otherwise deliver them twice in dev.
 */
import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import apiClient from '../utils/axios';

const TERMINAL_STATES = new Set(['completed', 'failed']);

// Backoff ladder (ms). The hook walks through this array and clamps at the
// last value. Tests override via `backoffSequenceMs` / `initialDelayMs`.
const DEFAULT_BACKOFF_MS = [1000, 2000, 4000, 8000, 10000];

// 30 min. Above this, the UI gives up and surfaces Retry.
const DEFAULT_TIMEOUT_MS = 30 * 60 * 1000;

// localStorage state — survives reloads so the spinner doesn't vanish.
const LS_KEY = 'renfield.kb.inflight';
const LS_MAX_ENTRIES = 20;
const LS_MAX_AGE_MS = 24 * 60 * 60 * 1000; // 24 h hard cap
const LS_STALE_TRIM_MS = 10 * 60 * 1000;   // 10 min — trim zombie entries

function readLSEntries() {
  try {
    const raw = window.localStorage.getItem(LS_KEY);
    if (!raw) return [];
    const parsed = JSON.parse(raw);
    if (!Array.isArray(parsed)) return [];
    const now = Date.now();
    return parsed
      .filter((e) => e && typeof e.docId === 'number' && typeof e.startedAt === 'number')
      .filter((e) => now - e.startedAt < LS_MAX_AGE_MS)
      .slice(0, LS_MAX_ENTRIES);
  } catch {
    return [];
  }
}

function writeLSEntries(entries) {
  try {
    const trimmed = entries.slice(0, LS_MAX_ENTRIES);
    window.localStorage.setItem(LS_KEY, JSON.stringify(trimmed));
  } catch {
    // Quota exceeded / privacy mode / SSR: just drop the write. State still
    // works in memory for the life of the page.
  }
}

function removeLSEntry(docId) {
  const entries = readLSEntries().filter((e) => e.docId !== docId);
  writeLSEntries(entries);
}

export function useDocumentPolling({
  onResolved,
  onTimeout,
  // Kept for back-compat with C1 callers; now only used as the fallback when
  // `backoffSequenceMs` isn't provided. Tests override to go fast.
  intervalMs,
  backoffSequenceMs,
  initialDelayMs,
  timeoutMs = DEFAULT_TIMEOUT_MS,
} = {}) {
  // Map<documentId, DocumentResponse>
  const [activeDocs, setActiveDocs] = useState({});
  const activeDocsRef = useRef(activeDocs);
  const onResolvedRef = useRef(onResolved);
  const onTimeoutRef = useRef(onTimeout);

  // Per-doc "first seen at" timestamp for the 30-min timeout.
  const trackedSinceRef = useRef(new Map());

  // Timer / abort state.
  const timerRef = useRef(null);
  const abortRef = useRef(null);
  // Step into DEFAULT_BACKOFF_MS. Reset to 0 on any progress signal.
  const backoffIndexRef = useRef(0);

  // Pick the effective backoff ladder once. An explicit `intervalMs`
  // (C1 contract) collapses the ladder to that single value. Memoising is
  // load-bearing: `doPoll` and the scheduling effect depend on the ladder
  // identity, and a fresh array on every render would cancel + reschedule
  // the timer from scratch on every poll, pinning the effective delay at
  // `firstDelay` forever. Callers frequently pass an inline array literal
  // (`backoffSequenceMs: [1000, 2000]`) so we key the memo off the
  // serialised value, not the array identity.
  const ladderKey = backoffSequenceMs
    ? backoffSequenceMs.join(',')
    : `i${intervalMs ?? ''}`;
  const backoffLadder = useMemo(
    () =>
      backoffSequenceMs ||
      (intervalMs != null ? [intervalMs] : DEFAULT_BACKOFF_MS),
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [ladderKey],
  );
  const firstDelay = initialDelayMs != null
    ? initialDelayMs
    : (intervalMs != null ? intervalMs : backoffLadder[0]);

  // ---------------------------------------------------------------------
  // Ref upkeep
  // ---------------------------------------------------------------------
  useEffect(() => {
    onResolvedRef.current = onResolved;
  }, [onResolved]);
  useEffect(() => {
    onTimeoutRef.current = onTimeout;
  }, [onTimeout]);
  useEffect(() => {
    activeDocsRef.current = activeDocs;
  }, [activeDocs]);

  // ---------------------------------------------------------------------
  // Hydration from localStorage on mount — one-shot
  // ---------------------------------------------------------------------
  useEffect(() => {
    const now = Date.now();
    const entries = readLSEntries().filter(
      (e) => now - e.startedAt < LS_STALE_TRIM_MS,
    );
    if (entries.length === 0) {
      // Drop any zombie entries older than the trim window, but never
      // re-hydrate them into state.
      writeLSEntries([]);
      return;
    }
    const hydrated = {};
    entries.forEach((e) => {
      hydrated[e.docId] = {
        id: e.docId,
        filename: e.filename || `#${e.docId}`,
        status: 'pending',
      };
      trackedSinceRef.current.set(e.docId, e.startedAt);
    });
    setActiveDocs((prev) => ({ ...hydrated, ...prev }));
    writeLSEntries(entries); // re-save the trimmed list
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  // ---------------------------------------------------------------------
  // Public API
  // ---------------------------------------------------------------------
  const track = useCallback((doc) => {
    if (!doc || TERMINAL_STATES.has(doc.status)) return;
    const now = Date.now();
    trackedSinceRef.current.set(doc.id, now);
    setActiveDocs((prev) => ({ ...prev, [doc.id]: doc }));
    // Persist right away so reload survives even before the first poll.
    const existing = readLSEntries().filter((e) => e.docId !== doc.id);
    writeLSEntries([
      { docId: doc.id, filename: doc.filename || '', startedAt: now },
      ...existing,
    ]);
    // Reset backoff so the first poll fires quickly.
    backoffIndexRef.current = 0;
  }, []);

  const forget = useCallback((id) => {
    trackedSinceRef.current.delete(id);
    removeLSEntry(id);
    setActiveDocs((prev) => {
      if (!(id in prev)) return prev;
      const next = { ...prev };
      delete next[id];
      return next;
    });
  }, []);

  // ---------------------------------------------------------------------
  // Polling machinery
  // ---------------------------------------------------------------------
  // Core batch fetch — one request, many ids, abortable.
  const doPoll = useCallback(async () => {
    const currentIds = Object.keys(activeDocsRef.current).map(Number);
    if (currentIds.length === 0) return;

    // 30-min timeout enforcement: drop any doc older than cap.
    const now = Date.now();
    const timedOutIds = currentIds.filter((id) => {
      const startedAt = trackedSinceRef.current.get(id);
      return startedAt && now - startedAt > timeoutMs;
    });
    if (timedOutIds.length) {
      const timedOutDocs = timedOutIds
        .map((id) => activeDocsRef.current[id])
        .filter(Boolean);
      setActiveDocs((prev) => {
        const next = { ...prev };
        for (const id of timedOutIds) delete next[id];
        return next;
      });
      for (const id of timedOutIds) {
        trackedSinceRef.current.delete(id);
        removeLSEntry(id);
      }
      if (onTimeoutRef.current) {
        for (const doc of timedOutDocs) onTimeoutRef.current(doc);
      }
    }
    const timedOutSet = new Set(timedOutIds);
    const aliveIds = currentIds.filter((id) => !timedOutSet.has(id));
    if (aliveIds.length === 0) return;

    const controller = new AbortController();
    // Abort any previous in-flight request before starting a new one.
    if (abortRef.current) abortRef.current.abort();
    abortRef.current = controller;

    let rows;
    try {
      const response = await apiClient.get('/api/knowledge/documents/batch', {
        params: { ids: aliveIds.join(',') },
        signal: controller.signal,
      });
      rows = response.data || [];
    } catch (err) {
      // AbortError is fine — means we moved on or the tab went hidden.
      if (err?.code === 'ERR_CANCELED' || err?.name === 'CanceledError' || err?.name === 'AbortError') {
        return;
      }
      console.warn('[useDocumentPolling] poll failed:', err);
      return;
    } finally {
      if (abortRef.current === controller) abortRef.current = null;
    }

    // Detect "anything changed" relative to our current view — any new stage,
    // new status, or page counter bump resets the backoff.
    const prev = activeDocsRef.current;
    let sawProgress = false;
    for (const row of rows) {
      const old = prev[row.id];
      if (!old) continue;
      if (
        old.status !== row.status ||
        old.stage !== row.stage ||
        (old.pages?.current ?? null) !== (row.pages?.current ?? null) ||
        (old.pages?.total ?? null) !== (row.pages?.total ?? null) ||
        (old.queue_position ?? null) !== (row.queue_position ?? null)
      ) {
        sawProgress = true;
      }
    }

    const resolvedThisTick = rows.filter((row) =>
      TERMINAL_STATES.has(row.status),
    );
    const seen = new Set(rows.map((row) => row.id));

    // Compute the next snapshot up-front so we can assign both state AND
    // the ref in lockstep. The `[activeDocs]` effect only fires after the
    // next commit, which is too late for the very next poll's sawProgress
    // comparison — and setActiveDocs' updater runs at commit time, so
    // reading the return value from inside the updater is also too late.
    const nextSnapshot = { ...prev };
    for (const row of rows) {
      if (TERMINAL_STATES.has(row.status)) {
        delete nextSnapshot[row.id];
      } else {
        nextSnapshot[row.id] = row;
      }
    }
    for (const id of aliveIds) {
      if (!seen.has(id)) delete nextSnapshot[id];
    }
    activeDocsRef.current = nextSnapshot;
    setActiveDocs(nextSnapshot);

    for (const row of resolvedThisTick) {
      trackedSinceRef.current.delete(row.id);
      removeLSEntry(row.id);
    }

    if (resolvedThisTick.length && onResolvedRef.current) {
      for (const row of resolvedThisTick) onResolvedRef.current(row);
    }

    // Reset backoff on progress, otherwise step forward.
    if (sawProgress || resolvedThisTick.length) {
      backoffIndexRef.current = 0;
    } else if (backoffIndexRef.current < backoffLadder.length - 1) {
      backoffIndexRef.current += 1;
    }
  }, [backoffLadder, timeoutMs]);

  // The long-lived schedule loop. We use setTimeout-per-tick (not
  // setInterval) because the next delay depends on the backoff state
  // computed inside the just-finished poll.
  useEffect(() => {
    let cancelled = false;

    const scheduleNext = (delayOverride) => {
      if (cancelled) return;
      if (timerRef.current) {
        clearTimeout(timerRef.current);
        timerRef.current = null;
      }
      if (Object.keys(activeDocsRef.current).length === 0) return;
      if (typeof document !== 'undefined' && document.visibilityState === 'hidden') {
        // Don't schedule while hidden; the visibility listener will
        // restart us when the tab comes back.
        return;
      }
      const delay = delayOverride != null
        ? delayOverride
        : backoffLadder[backoffIndexRef.current] ?? backoffLadder[backoffLadder.length - 1];
      timerRef.current = setTimeout(async () => {
        timerRef.current = null;
        await doPoll();
        scheduleNext();
      }, delay);
    };

    const onVisibilityChange = () => {
      if (typeof document === 'undefined') return;
      if (document.visibilityState === 'visible') {
        // Catch-up poll immediately, then resume the ladder.
        backoffIndexRef.current = 0;
        (async () => {
          await doPoll();
          scheduleNext();
        })();
      } else if (timerRef.current) {
        // Pause: clear pending timer, abort any in-flight request.
        clearTimeout(timerRef.current);
        timerRef.current = null;
        if (abortRef.current) abortRef.current.abort();
      }
    };

    if (typeof document !== 'undefined') {
      document.addEventListener('visibilitychange', onVisibilityChange);
    }

    // Kick off with the initial (short) delay so the first poll feels fast.
    if (Object.keys(activeDocsRef.current).length > 0) {
      scheduleNext(firstDelay);
    }

    return () => {
      cancelled = true;
      if (typeof document !== 'undefined') {
        document.removeEventListener('visibilitychange', onVisibilityChange);
      }
      if (timerRef.current) {
        clearTimeout(timerRef.current);
        timerRef.current = null;
      }
      if (abortRef.current) {
        abortRef.current.abort();
        abortRef.current = null;
      }
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [Object.keys(activeDocs).length === 0, doPoll]);

  return { activeDocs, track, forget };
}
