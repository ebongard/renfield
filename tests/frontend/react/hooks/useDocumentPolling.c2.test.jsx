/**
 * useDocumentPolling — C2 behaviour tests (#388).
 *
 * Covers matrix items 19 (backoff), 20 (Page Visibility), 21 (localStorage),
 * plus auxiliary coverage for the AbortController path and the timeout CTA.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { renderHook, act, waitFor } from '@testing-library/react';
import { http, HttpResponse } from 'msw';
import { server } from '../mocks/server.js';
import { TEST_CONFIG } from '../config.js';
import { useDocumentPolling } from '../../../../src/frontend/src/hooks/useDocumentPolling';

const BASE_URL = TEST_CONFIG.API_BASE_URL;
const BATCH_PATH = `${BASE_URL}/api/knowledge/documents/batch`;

function pendingDoc(id, overrides = {}) {
  return {
    id,
    filename: `f${id}.txt`,
    title: null,
    file_type: 'txt',
    file_size: 10,
    status: 'pending',
    error_message: null,
    chunk_count: 0,
    page_count: null,
    knowledge_base_id: 1,
    created_at: '2026-04-19T10:00:00Z',
    processed_at: null,
    stage: null,
    pages: null,
    queue_position: null,
    ...overrides,
  };
}

describe('useDocumentPolling C2 — backoff', () => {
  beforeEach(() => {
    server.resetHandlers();
    window.localStorage.clear();
  });

  it('walks through the backoff ladder when no progress is observed', async () => {
    // Record the wall-clock gap between successive batch requests.
    const stamps = [];
    server.use(
      http.get(BATCH_PATH, () => {
        stamps.push(performance.now());
        // Stable `processing` response — no change, so backoff should step up.
        return HttpResponse.json([pendingDoc(1, { status: 'processing', stage: 'parsing' })]);
      }),
    );
    const { result } = renderHook(() =>
      useDocumentPolling({
        initialDelayMs: 5,
        backoffSequenceMs: [5, 20, 80],
      }),
    );
    act(() => result.current.track(pendingDoc(1)));

    // Wait for 4 polls.
    await waitFor(() => expect(stamps.length).toBeGreaterThanOrEqual(4), { timeout: 2000 });

    // Gap between poll 1→2 should be near 5 ms (initial).
    // Gap 2→3 should be near 20 ms. Gap 3→4 should be near 80 ms.
    const gaps = stamps.slice(1).map((t, i) => t - stamps[i]);
    expect(gaps[0]).toBeLessThan(40);   // step 0 or 1
    expect(gaps[2]).toBeGreaterThan(gaps[0]); // later gaps grow
  });

  it('resets the backoff ladder on an observed progress change', async () => {
    let stage = 'parsing';
    let firstStableGap = null;
    const stamps = [];
    server.use(
      http.get(BATCH_PATH, () => {
        stamps.push(performance.now());
        return HttpResponse.json([pendingDoc(2, { status: 'processing', stage })]);
      }),
    );
    const { result } = renderHook(() =>
      useDocumentPolling({
        initialDelayMs: 5,
        backoffSequenceMs: [5, 60, 200],
      }),
    );
    act(() => result.current.track(pendingDoc(2)));

    // Let the backoff climb.
    await waitFor(() => expect(stamps.length).toBeGreaterThanOrEqual(3), { timeout: 2000 });
    firstStableGap = stamps[stamps.length - 1] - stamps[stamps.length - 2];

    // Flip the stage — next poll should go back to the short interval.
    stage = 'embedding';
    const stampsAtFlip = stamps.length;
    await waitFor(() => expect(stamps.length).toBeGreaterThanOrEqual(stampsAtFlip + 2), { timeout: 2000 });
    const gapAfterReset = stamps[stampsAtFlip + 1] - stamps[stampsAtFlip];
    expect(gapAfterReset).toBeLessThan(firstStableGap);
  });
});

describe('useDocumentPolling C2 — Page Visibility', () => {
  beforeEach(() => {
    server.resetHandlers();
    window.localStorage.clear();
  });

  afterEach(() => {
    // Restore visible state so other tests aren't stuck hidden.
    Object.defineProperty(document, 'visibilityState', { value: 'visible', configurable: true });
    document.dispatchEvent(new Event('visibilitychange'));
  });

  it('pauses polling while the tab is hidden and resumes on visibility', async () => {
    let calls = 0;
    server.use(
      http.get(BATCH_PATH, () => {
        calls += 1;
        return HttpResponse.json([pendingDoc(9, { status: 'processing' })]);
      }),
    );
    const { result } = renderHook(() =>
      useDocumentPolling({ initialDelayMs: 5, backoffSequenceMs: [5] }),
    );
    act(() => result.current.track(pendingDoc(9)));

    // Let at least one poll happen.
    await waitFor(() => expect(calls).toBeGreaterThanOrEqual(1));

    // Hide the tab. Capture the call count at the moment of hide.
    Object.defineProperty(document, 'visibilityState', { value: 'hidden', configurable: true });
    document.dispatchEvent(new Event('visibilitychange'));
    const callsAtHide = calls;

    // Give the loop time it would have used for several polls.
    await new Promise((r) => setTimeout(r, 60));
    expect(calls).toBe(callsAtHide);

    // Restore visibility — a catch-up poll should fire immediately.
    Object.defineProperty(document, 'visibilityState', { value: 'visible', configurable: true });
    document.dispatchEvent(new Event('visibilitychange'));
    await waitFor(() => expect(calls).toBeGreaterThan(callsAtHide));
  });
});

describe('useDocumentPolling C2 — localStorage persistence', () => {
  beforeEach(() => {
    server.resetHandlers();
    window.localStorage.clear();
  });

  it('writes inflight entries on track() and hydrates them on mount', async () => {
    server.use(
      http.get(BATCH_PATH, () =>
        HttpResponse.json([pendingDoc(7, { status: 'processing' })]),
      ),
    );
    const { result, unmount } = renderHook(() =>
      useDocumentPolling({ initialDelayMs: 5, backoffSequenceMs: [5] }),
    );
    act(() => result.current.track(pendingDoc(7, { filename: 'big.pdf' })));
    await waitFor(() => expect(result.current.activeDocs[7]).toBeTruthy());

    // Entry must be in localStorage.
    const stored = JSON.parse(window.localStorage.getItem('renfield.kb.inflight'));
    expect(stored).toEqual(
      expect.arrayContaining([
        expect.objectContaining({ docId: 7, filename: 'big.pdf' }),
      ]),
    );

    unmount();

    // Re-mount — the hook should hydrate from localStorage (since startedAt
    // is fresh, under the 10 min trim cap).
    const { result: result2 } = renderHook(() =>
      useDocumentPolling({ initialDelayMs: 5, backoffSequenceMs: [5] }),
    );
    await waitFor(() => expect(result2.current.activeDocs[7]).toBeTruthy());
  });

  it('clears localStorage for resolved docs', async () => {
    let returned = 'processing';
    server.use(
      http.get(BATCH_PATH, () =>
        HttpResponse.json([pendingDoc(8, { status: returned })]),
      ),
    );
    const { result } = renderHook(() =>
      useDocumentPolling({ initialDelayMs: 5, backoffSequenceMs: [5] }),
    );
    act(() => result.current.track(pendingDoc(8)));
    await waitFor(() => expect(result.current.activeDocs[8]?.status).toBe('processing'));
    expect(window.localStorage.getItem('renfield.kb.inflight')).toContain('8');

    returned = 'completed';
    await waitFor(() => expect(result.current.activeDocs[8]).toBeUndefined());
    // LS entry for 8 must be gone.
    await waitFor(() => {
      const raw = window.localStorage.getItem('renfield.kb.inflight') || '[]';
      const parsed = JSON.parse(raw);
      expect(parsed.find((e) => e.docId === 8)).toBeUndefined();
    });
  });

  it('drops entries older than the 10-min trim window on mount', async () => {
    const stale = [
      { docId: 77, filename: 'old.pdf', startedAt: Date.now() - 15 * 60 * 1000 },
    ];
    window.localStorage.setItem('renfield.kb.inflight', JSON.stringify(stale));

    renderHook(() =>
      useDocumentPolling({ initialDelayMs: 5, backoffSequenceMs: [5] }),
    );
    await waitFor(() => {
      const raw = window.localStorage.getItem('renfield.kb.inflight') || '[]';
      const parsed = JSON.parse(raw);
      expect(parsed.find((e) => e.docId === 77)).toBeUndefined();
    });
  });
});

describe('useDocumentPolling C2 — timeout CTA', () => {
  beforeEach(() => {
    server.resetHandlers();
    window.localStorage.clear();
  });

  it('drops docs past the per-doc timeout and fires onTimeout', async () => {
    server.use(
      http.get(BATCH_PATH, () =>
        HttpResponse.json([pendingDoc(123, { status: 'processing' })]),
      ),
    );
    const onTimeout = vi.fn();
    const { result } = renderHook(() =>
      useDocumentPolling({
        initialDelayMs: 5,
        backoffSequenceMs: [5],
        timeoutMs: 20,
        onTimeout,
      }),
    );
    act(() => result.current.track(pendingDoc(123)));

    await waitFor(() => expect(onTimeout).toHaveBeenCalledWith(
      expect.objectContaining({ id: 123 }),
    ));
    await waitFor(() => expect(result.current.activeDocs[123]).toBeUndefined());
  });
});
