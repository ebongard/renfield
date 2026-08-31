import { describe, it, expect } from 'vitest';
import { renderHook, waitFor, act } from '@testing-library/react';
import { http, HttpResponse } from 'msw';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { I18nextProvider } from 'react-i18next';
import type { ReactElement, ReactNode } from 'react';

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
import { useKnowledgeDocumentsQuery } from '../../../../src/frontend/src/api/resources/knowledge';
import { useConfirmSimbaProposal } from '../../../../src/frontend/src/api/resources/simbaIngest';

const BASE = TEST_CONFIG.API_BASE_URL;

function makeWrapper(client: QueryClient): (props: { children: ReactNode }) => ReactElement {
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

describe('Simba upload invalidates the knowledge documents list (in_simba status icon)', () => {
  it('a successful confirm refetches useKnowledgeDocumentsQuery so in_simba flips to true', async () => {
    // Regression: the confirm mutation only invalidated the Simba proposals list,
    // not keys.knowledge.all, so the doc row's Simba status icon stayed stale
    // after a successful upload until a manual reload.
    let inSimba = false;

    server.use(
      http.get(`${BASE}/api/knowledge/documents`, () =>
        HttpResponse.json([{ id: 5, filename: 'rechnung.pdf', in_simba: inSimba }]),
      ),
      http.post(`${BASE}/api/simba-ingest/42/confirm`, () => {
        inSimba = true; // the upload landed → backend now reports in_simba: true
        return HttpResponse.json({ success: true, message: 'An Simba übertragen' });
      }),
    );

    const client = new QueryClient({
      defaultOptions: {
        queries: { retry: false, gcTime: 0, staleTime: 0 },
        mutations: { retry: false },
      },
    });
    const wrapper = makeWrapper(client);

    const { result: docs } = renderHook(
      () => useKnowledgeDocumentsQuery({ knowledgeBaseId: null, statusFilter: 'all' }),
      { wrapper },
    );
    const { result: confirm } = renderHook(() => useConfirmSimbaProposal(), { wrapper });

    await waitFor(() => expect(docs.current.data?.[0]?.in_simba).toBe(false));

    await act(async () => {
      await confirm.current.mutateAsync({
        id: 42,
        category: 'Belege',
        type: 'Eingangsrechnung',
        description: 'Rechnung',
        month: 3,
        year: 2026,
      });
    });

    // The documents list refetched itself (no explicit refetch) and the icon flips.
    await waitFor(() => expect(docs.current.data?.[0]?.in_simba).toBe(true));
  });

  it('an already-in-Simba confirm (success:false) does NOT refetch the documents list', async () => {
    // Guard the gate: only a genuine upload should refetch knowledge; an
    // already-in-Simba / rejected confirm returns success:false and changes no
    // document state, so it must not thrash the documents query.
    let docFetches = 0;

    server.use(
      http.get(`${BASE}/api/knowledge/documents`, () => {
        docFetches += 1;
        return HttpResponse.json([{ id: 5, filename: 'rechnung.pdf', in_simba: false }]);
      }),
      http.post(`${BASE}/api/simba-ingest/42/confirm`, () =>
        HttpResponse.json({ success: false, message: 'already_in_simba', already_in_simba: true }),
      ),
    );

    const client = new QueryClient({
      defaultOptions: {
        queries: { retry: false, gcTime: 0, staleTime: 0 },
        mutations: { retry: false },
      },
    });
    const wrapper = makeWrapper(client);

    const { result: docs } = renderHook(
      () => useKnowledgeDocumentsQuery({ knowledgeBaseId: null, statusFilter: 'all' }),
      { wrapper },
    );
    const { result: confirm } = renderHook(() => useConfirmSimbaProposal(), { wrapper });

    await waitFor(() => expect(docs.current.data).toHaveLength(1));
    const fetchesAfterInitial = docFetches;

    await act(async () => {
      await confirm.current.mutateAsync({
        id: 42,
        category: 'Belege',
        type: 'Eingangsrechnung',
        description: 'Rechnung',
        month: 3,
        year: 2026,
      });
    });

    // No extra documents fetch was triggered by the non-upload confirm.
    await act(async () => {
      await new Promise((r) => setTimeout(r, 20));
    });
    expect(docFetches).toBe(fetchesAfterInitial);
  });
});
