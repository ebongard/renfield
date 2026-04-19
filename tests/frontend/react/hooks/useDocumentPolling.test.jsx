/**
 * useDocumentPolling — unit tests for the C1 minimal polling hook (#388).
 *
 * Matrix items:
 *   15 upload → 202 → polling → completed
 *   16 upload → 202 → polling → failed
 *
 * Full C2 behaviour (backoff, Visibility API, localStorage, progressbar)
 * has its own coverage later.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest';
import { renderHook, act, waitFor } from '@testing-library/react';
import { http, HttpResponse } from 'msw';
import { server } from '../mocks/server.js';
import { TEST_CONFIG } from '../config.js';
import { useDocumentPolling } from '../../../../src/frontend/src/hooks/useDocumentPolling';

const BASE_URL = TEST_CONFIG.API_BASE_URL;
const BATCH_PATH = `${BASE_URL}/api/knowledge/documents/batch`;

// Tiny interval so the tests run in milliseconds without fake timers, which
// interact badly with MSW request interception and RTL's waitFor.
const TEST_INTERVAL = 10;

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

describe('useDocumentPolling', () => {
  beforeEach(() => {
    server.resetHandlers();
  });

  it('starts idle with no active docs', () => {
    const { result } = renderHook(() => useDocumentPolling({ intervalMs: TEST_INTERVAL }));
    expect(Object.keys(result.current.activeDocs)).toHaveLength(0);
  });

  it('tracks a pending doc and polls the batch endpoint', async () => {
    const batchCalls = [];
    server.use(
      http.get(BATCH_PATH, ({ request }) => {
        const url = new URL(request.url);
        batchCalls.push(url.searchParams.get('ids'));
        return HttpResponse.json([pendingDoc(42, { status: 'processing', stage: 'ocr' })]);
      }),
    );
    const { result } = renderHook(() => useDocumentPolling({ intervalMs: TEST_INTERVAL }));

    act(() => {
      result.current.track(pendingDoc(42));
    });

    await waitFor(() => {
      expect(result.current.activeDocs[42]?.status).toBe('processing');
      expect(result.current.activeDocs[42]?.stage).toBe('ocr');
    });
    expect(batchCalls.length).toBeGreaterThanOrEqual(1);
    expect(batchCalls[0]).toBe('42');
  });

  it('resolves a doc and fires onResolved when status=completed', async () => {
    let returned = 'processing';
    server.use(
      http.get(BATCH_PATH, () =>
        HttpResponse.json([pendingDoc(99, { status: returned })]),
      ),
    );
    const onResolved = vi.fn();
    const { result } = renderHook(() =>
      useDocumentPolling({ onResolved, intervalMs: TEST_INTERVAL }),
    );
    act(() => result.current.track(pendingDoc(99)));

    await waitFor(() => expect(result.current.activeDocs[99]?.status).toBe('processing'));
    returned = 'completed';
    await waitFor(() => expect(result.current.activeDocs[99]).toBeUndefined());
    await waitFor(() =>
      expect(onResolved).toHaveBeenCalledWith(
        expect.objectContaining({ id: 99, status: 'completed' }),
      ),
    );
  });

  it('resolves with status=failed and fires onResolved with error_message', async () => {
    let returned = 'processing';
    server.use(
      http.get(BATCH_PATH, () =>
        HttpResponse.json([
          pendingDoc(7, { status: returned, error_message: returned === 'failed' ? 'boom' : null }),
        ]),
      ),
    );
    const onResolved = vi.fn();
    const { result } = renderHook(() =>
      useDocumentPolling({ onResolved, intervalMs: TEST_INTERVAL }),
    );
    act(() => result.current.track(pendingDoc(7)));

    await waitFor(() => expect(result.current.activeDocs[7]?.status).toBe('processing'));
    returned = 'failed';
    await waitFor(() =>
      expect(onResolved).toHaveBeenCalledWith(
        expect.objectContaining({ id: 7, status: 'failed', error_message: 'boom' }),
      ),
    );
  });

  it('drops docs the server no longer returns (e.g. deleted mid-poll)', async () => {
    server.use(http.get(BATCH_PATH, () => HttpResponse.json([])));
    const { result } = renderHook(() =>
      useDocumentPolling({ intervalMs: TEST_INTERVAL }),
    );
    act(() => result.current.track(pendingDoc(13)));
    await waitFor(() => expect(result.current.activeDocs[13]).toBeUndefined());
  });

  it('survives a transient network error without losing tracked docs', async () => {
    let call = 0;
    server.use(
      http.get(BATCH_PATH, () => {
        call += 1;
        if (call === 1) return HttpResponse.error();
        return HttpResponse.json([pendingDoc(5, { status: 'processing' })]);
      }),
    );
    const { result } = renderHook(() =>
      useDocumentPolling({ intervalMs: TEST_INTERVAL }),
    );
    act(() => result.current.track(pendingDoc(5)));

    await waitFor(() => expect(call).toBeGreaterThanOrEqual(2));
    await waitFor(() => expect(result.current.activeDocs[5]?.status).toBe('processing'));
  });

  it('keeps polling when a doc resolves + a new one is tracked in the same tick (same length)', async () => {
    // Regression: the poll loop used to close over a stale activeDocs
    // snapshot, so if A resolved and B was tracked in the same React
    // batch, the next XHR would still target A and never query B.
    const requestedIds = [];
    let aStatus = 'processing';
    server.use(
      http.get(BATCH_PATH, ({ request }) => {
        const url = new URL(request.url);
        const ids = url.searchParams.get('ids') || '';
        requestedIds.push(ids);
        const rows = [];
        for (const raw of ids.split(',').filter(Boolean)) {
          const id = Number(raw);
          if (id === 1) rows.push(pendingDoc(1, { status: aStatus }));
          if (id === 2) rows.push(pendingDoc(2, { status: 'processing' }));
        }
        return HttpResponse.json(rows);
      }),
    );
    const { result } = renderHook(() => useDocumentPolling({ intervalMs: TEST_INTERVAL }));
    act(() => result.current.track(pendingDoc(1)));
    await waitFor(() => expect(result.current.activeDocs[1]?.status).toBe('processing'));

    // Simulate A resolving and B being tracked in the same React batch.
    aStatus = 'completed';
    act(() => {
      result.current.track(pendingDoc(2));
    });

    // Eventually the server should be asked about id 2.
    await waitFor(() => {
      expect(requestedIds.some((q) => q.split(',').includes('2'))).toBe(true);
    });
    await waitFor(() => expect(result.current.activeDocs[2]?.status).toBe('processing'));
  });
});
