/**
 * useBestaetigt — server-backed acknowledgement state (notifier-ledger migration).
 *
 * The hook now writes to the obligation ledger via react-query mutations and
 * layers an optimistic override over the server `confirmed` flag for the 5s undo
 * window. Tests mock axios (post/delete) and wrap the hook in a QueryClient.
 *
 * Load-bearing guarantees retained: confirm-one ≠ confirm-all (per-id), the 5s
 * window + Esc/undo revert, and reopen. New: server-flag layering + the
 * one-time localStorage→server migration.
 */
import React from 'react';
import { describe, it, expect, beforeEach, vi, afterEach } from 'vitest';
import { act, renderHook } from '@testing-library/react';
import { QueryClientProvider } from '@tanstack/react-query';

import { useBestaetigt, UNDO_WINDOW_MS } from '../../../../src/frontend/src/hooks/useBestaetigt';
import { createTestQueryClient } from '../test-utils';
import apiClient from '../../../../src/frontend/src/utils/axios';

vi.mock('../../../../src/frontend/src/utils/axios', () => ({
  default: {
    post: vi.fn().mockResolvedValue({ data: { confirmed: true } }),
    delete: vi.fn().mockResolvedValue({ data: { confirmed: false } }),
    get: vi.fn().mockResolvedValue({ data: [] }),
  },
  extractApiError: (_e: unknown, fallback: string) => fallback,
  extractFieldErrors: () => ({}),
}));

const mockedPost = vi.mocked(apiClient.post);
const mockedDelete = vi.mocked(apiClient.delete);

function wrapper({ children }: { children: React.ReactNode }) {
  return React.createElement(QueryClientProvider, { client: createTestQueryClient() }, children);
}

const render = () => renderHook(() => useBestaetigt(), { wrapper });

// react-query fires the mutationFn asynchronously; flush microtasks + the
// scheduler so the post/delete spy registers before we assert on it.
const flush = () => act(async () => {
  await vi.advanceTimersByTimeAsync(0);
});

describe('useBestaetigt (server-backed)', () => {
  beforeEach(() => {
    localStorage.clear();
    mockedPost.mockClear();
    mockedDelete.mockClear();
    vi.useFakeTimers();
  });
  afterEach(() => {
    vi.useRealTimers();
  });

  it('REGRESSION (D4): confirming one obligation does not confirm another', () => {
    const { result } = render();
    act(() => result.current.confirm(1));
    expect(result.current.isConfirmed(1)).toBe(true);
    expect(result.current.isConfirmed(2)).toBe(false);
  });

  it('confirm POSTs to the obligation ledger', async () => {
    const { result } = render();
    act(() => result.current.confirm(7));
    await flush();
    expect(mockedPost).toHaveBeenCalledWith('/api/atoms/obligations/7/confirm');
  });

  it('opens a 5s undo window then the confirmation sticks', () => {
    const { result } = render();
    act(() => result.current.confirm(1));
    expect(result.current.pending).toBe(1);
    act(() => vi.advanceTimersByTime(UNDO_WINDOW_MS));
    expect(result.current.pending).toBeNull();
    expect(result.current.isConfirmed(1)).toBe(true);
  });

  it('undo within the window reverts and DELETEs', async () => {
    const { result } = render();
    act(() => result.current.confirm(1));
    act(() => result.current.undo(1));
    await flush();
    expect(result.current.isConfirmed(1)).toBe(false);
    expect(result.current.pending).toBeNull();
    expect(mockedDelete).toHaveBeenCalledWith('/api/atoms/obligations/1/confirm');
  });

  it('Esc within the window reverts (D-FLOW-1 / A11Y)', () => {
    const { result } = render();
    act(() => result.current.confirm(1));
    act(() => {
      window.dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape' }));
    });
    expect(result.current.isConfirmed(1)).toBe(false);
  });

  it('reopen un-acknowledges without opening a toast and DELETEs', async () => {
    const { result } = render();
    act(() => result.current.confirm(1));
    act(() => vi.advanceTimersByTime(UNDO_WINDOW_MS));
    act(() => result.current.reopen(1));
    await flush();
    expect(result.current.isConfirmed(1)).toBe(false);
    expect(result.current.pending).toBeNull();
    expect(mockedDelete).toHaveBeenCalledWith('/api/atoms/obligations/1/confirm');
  });

  it('falls back to the server confirmed flag when no local override', () => {
    const { result } = render();
    expect(result.current.isConfirmed(99, true)).toBe(true);   // server says confirmed
    expect(result.current.isConfirmed(99, false)).toBe(false);
  });

  it('migrates legacy localStorage ids to the server once, then clears the store', async () => {
    localStorage.setItem('renfield.obligations.bestaetigt', JSON.stringify([42, 43]));
    const { result } = render();
    await flush();
    expect(mockedPost).toHaveBeenCalledWith('/api/atoms/obligations/42/confirm');
    expect(mockedPost).toHaveBeenCalledWith('/api/atoms/obligations/43/confirm');
    expect(localStorage.getItem('renfield.obligations.bestaetigt')).toBeNull();
    expect(localStorage.getItem('renfield.obligations.bestaetigt.migrated')).toBe('1');
    expect(result.current.isConfirmed(42)).toBe(true);  // optimistic override
  });

  it('does not re-run the migration once the flag is set', () => {
    localStorage.setItem('renfield.obligations.bestaetigt.migrated', '1');
    localStorage.setItem('renfield.obligations.bestaetigt', JSON.stringify([99]));
    render();
    expect(mockedPost).not.toHaveBeenCalled();
  });
});
