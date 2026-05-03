import { describe, it, expect } from 'vitest';
import { renderHook, waitFor, act } from '@testing-library/react';
import { http, HttpResponse } from 'msw';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { I18nextProvider } from 'react-i18next';
import type { ReactNode } from 'react';

import { server } from '../mocks/server';
import { TEST_CONFIG } from '../config';
import i18n from '../../../../src/frontend/src/i18n';
import {
  useMemoriesQuery,
  useCreateMemory,
} from '../../../../src/frontend/src/api/resources/memories';
import type {
  Memory,
  MemoryInput,
} from '../../../../src/frontend/src/api/resources/memories';

const BASE = TEST_CONFIG.API_BASE_URL;

function makeWrapper(client: QueryClient): (props: { children: ReactNode }) => JSX.Element {
  return function Wrapper({ children }: { children: ReactNode }) {
    return (
      <I18nextProvider i18n={i18n}>
        <QueryClientProvider client={client}>{children}</QueryClientProvider>
      </I18nextProvider>
    );
  };
}

describe('Resource invalidation contract (memories as canonical example)', () => {
  it('mutating via useCreateMemory triggers a refetch of useMemoriesQuery without explicit refetch()', async () => {
    let memoriesState: Memory[] = [
      {
        id: 1,
        content: 'first',
        category: 'fact',
        importance: 0.5,
        access_count: 0,
        created_at: '2026-01-01T00:00:00Z',
      },
    ];

    server.use(
      http.get(`${BASE}/api/memory`, () =>
        HttpResponse.json({ memories: memoriesState, total: memoriesState.length }),
      ),
      http.post(`${BASE}/api/memory`, async ({ request }) => {
        const body = (await request.json()) as MemoryInput;
        const created: Memory = {
          id: memoriesState.length + 1,
          content: body.content,
          category: body.category,
          importance: body.importance,
          access_count: 0,
          created_at: '2026-01-02T00:00:00Z',
        };
        memoriesState = [...memoriesState, created];
        return HttpResponse.json(created, { status: 201 });
      }),
    );

    const client = new QueryClient({
      defaultOptions: {
        queries: { retry: false, gcTime: 0, staleTime: 0 },
        mutations: { retry: false },
      },
    });
    const wrapper = makeWrapper(client);

    const { result: queryResult } = renderHook(() => useMemoriesQuery(null), { wrapper });
    const { result: mutationResult } = renderHook(() => useCreateMemory(), { wrapper });

    // Initial fetch
    await waitFor(() => expect(queryResult.current.data?.memories).toHaveLength(1));

    // Mutate — invalidation should trigger refetch automatically
    await act(async () => {
      await mutationResult.current.mutateAsync({
        content: 'second',
        category: 'fact',
        importance: 0.7,
      });
    });

    // The list now shows the new item without anyone calling refetch()
    await waitFor(() => expect(queryResult.current.data?.memories).toHaveLength(2));
    expect(queryResult.current.data?.memories[1].content).toBe('second');
  });
});
