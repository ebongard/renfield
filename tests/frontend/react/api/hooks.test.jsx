import { describe, it, expect, beforeEach } from 'vitest';
import { renderHook, waitFor, act } from '@testing-library/react';
import { http, HttpResponse } from 'msw';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { I18nextProvider } from 'react-i18next';

import { server } from '../mocks/server.js';
import { TEST_CONFIG } from '../config.js';
import i18n from '../../../../src/frontend/src/i18n';
import apiClient from '../../../../src/frontend/src/utils/axios';
import { useApiQuery, useApiMutation } from '../../../../src/frontend/src/api/hooks';

const BASE = TEST_CONFIG.API_BASE_URL;

function makeWrapper() {
  const client = new QueryClient({
    defaultOptions: {
      queries: { retry: false, gcTime: 0, staleTime: 0 },
      mutations: { retry: false },
    },
  });
  return ({ children }) => (
    <I18nextProvider i18n={i18n}>
      <QueryClientProvider client={client}>{children}</QueryClientProvider>
    </I18nextProvider>
  );
}

describe('useApiQuery', () => {
  beforeEach(() => {
    i18n.changeLanguage('en');
  });

  it('formats errorMessage from 422 Pydantic detail array', async () => {
    server.use(
      http.get(`${BASE}/api/test-422`, () =>
        HttpResponse.json(
          {
            detail: [
              { loc: ['body', 'username'], msg: 'username already taken', type: 'value_error' },
            ],
          },
          { status: 422 },
        ),
      ),
    );

    const { result } = renderHook(
      () =>
        useApiQuery(
          {
            queryKey: ['test-422'],
            queryFn: () => apiClient.get('/api/test-422').then((r) => r.data),
          },
          'common.error',
        ),
      { wrapper: makeWrapper() },
    );

    await waitFor(() => expect(result.current.isError).toBe(true));
    expect(result.current.errorMessage).toContain('username already taken');
  });

  it('formats errorMessage from 500 with string detail', async () => {
    server.use(
      http.get(`${BASE}/api/test-500`, () =>
        HttpResponse.json({ detail: 'database is down' }, { status: 500 }),
      ),
    );

    const { result } = renderHook(
      () =>
        useApiQuery(
          {
            queryKey: ['test-500'],
            queryFn: () => apiClient.get('/api/test-500').then((r) => r.data),
          },
          'common.error',
        ),
      { wrapper: makeWrapper() },
    );

    await waitFor(() => expect(result.current.isError).toBe(true));
    expect(result.current.errorMessage).toBe('database is down');
  });

  it('falls back to translated key when no detail', async () => {
    server.use(
      http.get(`${BASE}/api/test-bare`, () => HttpResponse.json({}, { status: 503 })),
    );

    const { result } = renderHook(
      () =>
        useApiQuery(
          {
            queryKey: ['test-bare'],
            queryFn: () => apiClient.get('/api/test-bare').then((r) => r.data),
          },
          'common.error',
        ),
      { wrapper: makeWrapper() },
    );

    await waitFor(() => expect(result.current.isError).toBe(true));
    // Fallback message comes from i18n key 'common.error'
    expect(result.current.errorMessage).toBeTruthy();
    expect(result.current.errorMessage).not.toBe('common.error'); // it should have been translated
  });

  it('returns errorMessage = null when no error', async () => {
    server.use(
      http.get(`${BASE}/api/test-ok`, () => HttpResponse.json({ ok: true })),
    );

    const { result } = renderHook(
      () =>
        useApiQuery(
          {
            queryKey: ['test-ok'],
            queryFn: () => apiClient.get('/api/test-ok').then((r) => r.data),
          },
          'common.error',
        ),
      { wrapper: makeWrapper() },
    );

    await waitFor(() => expect(result.current.isLoading).toBe(false));
    expect(result.current.errorMessage).toBeNull();
    expect(result.current.error).toBeNull();
  });
});

describe('useApiMutation', () => {
  beforeEach(() => {
    i18n.changeLanguage('en');
  });

  it('formats errorMessage on mutation failure', async () => {
    server.use(
      http.post(`${BASE}/api/test-mut-fail`, () =>
        HttpResponse.json({ detail: 'forbidden write' }, { status: 403 }),
      ),
    );

    const { result } = renderHook(
      () =>
        useApiMutation(
          {
            mutationFn: (vars) =>
              apiClient.post('/api/test-mut-fail', vars).then((r) => r.data),
          },
          'common.error',
        ),
      { wrapper: makeWrapper() },
    );

    await act(async () => {
      try {
        await result.current.mutateAsync({ x: 1 });
      } catch {
        // expected to throw
      }
    });

    await waitFor(() => expect(result.current.isError).toBe(true));
    expect(result.current.errorMessage).toBe('forbidden write');
  });

  it('exposes fieldErrors map for 422 responses', async () => {
    server.use(
      http.post(`${BASE}/api/test-mut-422`, () =>
        HttpResponse.json(
          {
            detail: [
              { loc: ['body', 'email'], msg: 'invalid email format', type: 'value_error' },
              { loc: ['body', 'password'], msg: 'too short', type: 'value_error' },
            ],
          },
          { status: 422 },
        ),
      ),
    );

    const { result } = renderHook(
      () =>
        useApiMutation(
          {
            mutationFn: (vars) =>
              apiClient.post('/api/test-mut-422', vars).then((r) => r.data),
          },
          'common.error',
        ),
      { wrapper: makeWrapper() },
    );

    await act(async () => {
      try {
        await result.current.mutateAsync({});
      } catch {
        // expected
      }
    });

    await waitFor(() => expect(result.current.isError).toBe(true));
    expect(result.current.fieldErrors).toEqual({
      email: 'invalid email format',
      password: 'too short',
    });
  });

  it('errorMessage resets to null on next mutate call', async () => {
    let attempt = 0;
    server.use(
      http.post(`${BASE}/api/test-mut-toggle`, () => {
        attempt += 1;
        if (attempt === 1) {
          return HttpResponse.json({ detail: 'first failed' }, { status: 500 });
        }
        return HttpResponse.json({ ok: true });
      }),
    );

    const { result } = renderHook(
      () =>
        useApiMutation(
          {
            mutationFn: (vars) =>
              apiClient.post('/api/test-mut-toggle', vars).then((r) => r.data),
          },
          'common.error',
        ),
      { wrapper: makeWrapper() },
    );

    await act(async () => {
      try {
        await result.current.mutateAsync({});
      } catch {
        // expected
      }
    });
    await waitFor(() => expect(result.current.errorMessage).toBe('first failed'));

    await act(async () => {
      await result.current.mutateAsync({});
    });

    await waitFor(() => expect(result.current.isError).toBe(false));
    expect(result.current.errorMessage).toBeNull();
  });

  it('caller still has raw AxiosError for edge cases', async () => {
    server.use(
      http.post(`${BASE}/api/test-mut-raw`, () =>
        HttpResponse.json({ detail: 'oops' }, { status: 418 }),
      ),
    );

    const { result } = renderHook(
      () =>
        useApiMutation(
          {
            mutationFn: (vars) =>
              apiClient.post('/api/test-mut-raw', vars).then((r) => r.data),
          },
          'common.error',
        ),
      { wrapper: makeWrapper() },
    );

    await act(async () => {
      try {
        await result.current.mutateAsync({});
      } catch {
        // expected
      }
    });

    await waitFor(() => expect(result.current.isError).toBe(true));
    expect(result.current.error?.response?.status).toBe(418);
  });
});
