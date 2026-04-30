import { describe, it, expect } from 'vitest';
import { queryClient } from '../../../../src/frontend/src/api/queryClient';

describe('queryClient defaults', () => {
  it('does not retry mutations (regression guard for E11/D2 — duplicate writes)', () => {
    const defaults = queryClient.getDefaultOptions();
    expect(defaults.mutations?.retry).toBe(0);
  });

  it('disables refetchOnWindowFocus globally', () => {
    const defaults = queryClient.getDefaultOptions();
    expect(defaults.queries?.refetchOnWindowFocus).toBe(false);
  });

  it('uses a function for query retry that bails on 4xx', () => {
    const defaults = queryClient.getDefaultOptions();
    expect(typeof defaults.queries?.retry).toBe('function');

    const retryFn = defaults.queries.retry;
    const make4xx = (status) => ({ response: { status } });

    // 4xx should NOT retry
    expect(retryFn(0, make4xx(401))).toBe(false);
    expect(retryFn(0, make4xx(404))).toBe(false);
    expect(retryFn(0, make4xx(422))).toBe(false);
  });

  it('retries 5xx and network errors at most once', () => {
    const defaults = queryClient.getDefaultOptions();
    const retryFn = defaults.queries.retry;
    const make5xx = (status) => ({ response: { status } });

    // First failure: retry
    expect(retryFn(0, make5xx(500))).toBe(true);
    expect(retryFn(0, make5xx(503))).toBe(true);
    // Network error (no response): retry
    expect(retryFn(0, new Error('Network Error'))).toBe(true);

    // Second failure: stop
    expect(retryFn(1, make5xx(500))).toBe(false);
    expect(retryFn(1, new Error('Network Error'))).toBe(false);
  });

  it('uses 30s default staleTime', () => {
    const defaults = queryClient.getDefaultOptions();
    expect(defaults.queries?.staleTime).toBe(30_000);
  });
});
