import { describe, it, expect } from 'vitest';
import type { DefaultOptions } from '@tanstack/react-query';
import { queryClient } from '../../../../src/frontend/src/api/queryClient';

// React Query's `retry` option for queries can be either a boolean, a number,
// or a function `(failureCount, error) => boolean`. The local `queryClient`
// always installs the function variant; the cast here narrows the public
// `DefaultOptions` shape down to that concrete callable signature.
type RetryFn = (failureCount: number, error: unknown) => boolean;

function getQueryRetry(defaults: DefaultOptions): RetryFn {
  const retry = defaults.queries?.retry;
  if (typeof retry !== 'function') {
    throw new Error(`expected queries.retry to be a function, got ${typeof retry}`);
  }
  return retry as RetryFn;
}

interface HttpLikeError {
  response: { status: number };
}

const make4xx = (status: number): HttpLikeError => ({ response: { status } });
const make5xx = (status: number): HttpLikeError => ({ response: { status } });

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

    const retryFn = getQueryRetry(defaults);

    // 4xx should NOT retry
    expect(retryFn(0, make4xx(401))).toBe(false);
    expect(retryFn(0, make4xx(404))).toBe(false);
    expect(retryFn(0, make4xx(422))).toBe(false);
  });

  it('retries 5xx and network errors at most once', () => {
    const defaults = queryClient.getDefaultOptions();
    const retryFn = getQueryRetry(defaults);

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
