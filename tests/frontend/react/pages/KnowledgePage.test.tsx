/**
 * KnowledgePage — the two previously-untested pieces of new logic flagged in
 * /review: the inbound ?doc= deep-link auto-expand (D3) and the reindex →
 * facts-cache invalidation (D5). German default.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest';
import { screen, waitFor, fireEvent } from '@testing-library/react';
import KnowledgePage from '../../../../src/frontend/src/pages/KnowledgePage';
import { renderWithProviders, createTestQueryClient, createMockResponse } from '../test-utils';
import apiClient from '../../../../src/frontend/src/utils/axios';

vi.mock('../../../../src/frontend/src/utils/axios', () => ({
  default: { get: vi.fn(), post: vi.fn(), delete: vi.fn() },
  extractApiError: (_e: unknown, fallback: string) => fallback,
  extractFieldErrors: () => ({}),
}));

const mockedGet = vi.mocked(apiClient.get);
const mockedPost = vi.mocked(apiClient.post);

const DOC = {
  id: 5, filename: 'steuer.pdf', title: 'Steuerbescheid', status: 'completed',
  file_type: 'pdf', chunk_count: 3, page_count: 4,
  created_at: '2026-01-02T00:00:00', document_date: '2025-12-16',
  in_paperless: true, in_simba: false,
};

function wire() {
  mockedGet.mockImplementation((url: string) => {
    if (url.includes('/api/knowledge/documents')) return Promise.resolve(createMockResponse([DOC]));
    if (url.includes('/api/knowledge/bases')) return Promise.resolve(createMockResponse([]));
    if (url.includes('/api/knowledge/stats')) {
      return Promise.resolve(createMockResponse({
        document_count: 1, completed_documents: 1, chunk_count: 3, knowledge_base_count: 0,
      }));
    }
    if (url.includes('/api/config/features')) {
      return Promise.resolve(createMockResponse({ schicht_a_extraction_enabled: true }));
    }
    if (url.includes('/facts')) return Promise.resolve(createMockResponse([]));
    return Promise.resolve(createMockResponse([]));
  });
  // Async reindex returns 202 + the doc row in 'pending' (trackDocument polls it).
  mockedPost.mockResolvedValue(createMockResponse({ ...DOC, status: 'pending' }, 202));
}

describe('KnowledgePage Schicht A integration', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    mockedGet.mockReset();
    mockedPost.mockReset();
    Element.prototype.scrollIntoView = vi.fn();
    wire();
  });

  it('auto-expands the targeted document\'s Fakten panel on ?doc= deep link (D3)', async () => {
    renderWithProviders(<KnowledgePage />, { route: '/knowledge?doc=5' });
    const toggle = await screen.findByRole('button', { name: /Fakten/ });
    await waitFor(() => expect(toggle).toHaveAttribute('aria-expanded', 'true'));
    expect(Element.prototype.scrollIntoView).toHaveBeenCalled();
  });

  // The chat's source chips link here. The list is one page of the corpus;
  // a document beyond it used to open the page and do nothing.
  it('loads a deep-linked document that is NOT in the fetched list, by its id', async () => {
    const FAR = { ...DOC, id: 977, filename: 'alt.pdf', title: 'Alter Vertrag' };
    const byId: string[] = [];
    mockedGet.mockImplementation((url: string) => {
      if (url === '/api/knowledge/documents/977') {
        byId.push(url);
        return Promise.resolve(createMockResponse(FAR));
      }
      if (url.includes('/api/knowledge/documents')) return Promise.resolve(createMockResponse([DOC]));
      if (url.includes('/api/knowledge/stats')) {
        return Promise.resolve(createMockResponse({
          document_count: 500, completed_documents: 500, chunk_count: 3, knowledge_base_count: 0,
        }));
      }
      if (url.includes('/api/config/features')) {
        return Promise.resolve(createMockResponse({ schicht_a_extraction_enabled: true }));
      }
      return Promise.resolve(createMockResponse([]));
    });

    renderWithProviders(<KnowledgePage />, { route: '/knowledge?doc=977' });

    // The row exists, is the FIRST row, and its Fakten panel is open.
    await waitFor(() => expect(document.getElementById('doc-977')).not.toBeNull());
    expect(byId).toEqual(['/api/knowledge/documents/977']);
    const rows = Array.from(document.querySelectorAll('[id^="doc-"]')).map((el) => el.id);
    expect(rows).toEqual(['doc-977', 'doc-5']);
    const target = document.getElementById('doc-977') as HTMLElement;
    await waitFor(() => {
      const toggle = target.querySelector('button[aria-expanded]');
      expect(toggle).toHaveAttribute('aria-expanded', 'true');
    });
    await waitFor(() => expect(Element.prototype.scrollIntoView).toHaveBeenCalled());
  });

  it('does not fetch by id when the deep-linked document is already in the list', async () => {
    renderWithProviders(<KnowledgePage />, { route: '/knowledge?doc=5' });
    const toggle = await screen.findByRole('button', { name: /Fakten/ });
    await waitFor(() => expect(toggle).toHaveAttribute('aria-expanded', 'true'));
    const urls = mockedGet.mock.calls.map((c) => String(c[0]));
    expect(urls.filter((u) => /\/api\/knowledge\/documents\/\d+$/.test(u))).toEqual([]);
  });

  it('shows no extra row when the deep-linked document is not readable', async () => {
    mockedGet.mockImplementation((url: string) => {
      if (url === '/api/knowledge/documents/977') {
        return Promise.reject(Object.assign(new Error('forbidden'), { response: { status: 403 } }));
      }
      if (url.includes('/api/knowledge/documents')) return Promise.resolve(createMockResponse([DOC]));
      if (url.includes('/api/config/features')) {
        return Promise.resolve(createMockResponse({ schicht_a_extraction_enabled: true }));
      }
      return Promise.resolve(createMockResponse([]));
    });

    renderWithProviders(<KnowledgePage />, { route: '/knowledge?doc=977' });

    await waitFor(() => expect(document.getElementById('doc-5')).not.toBeNull());
    await waitFor(() => expect(
      mockedGet.mock.calls.some((c) => c[0] === '/api/knowledge/documents/977'),
    ).toBe(true));
    expect(document.getElementById('doc-977')).toBeNull();
  });

  // Every knowledge mutation invalidates `['knowledge']`, the by-id entry
  // included. react-query keeps the LAST successful data when the refetch
  // fails -- so the pinned row must follow the error, not the stale data.
  it.each([
    ['deleted (404)', 404],
    ['no longer readable (403)', 403],
  ])('drops the pinned row once the document is %s on refetch', async (_label, status) => {
    const FAR = { ...DOC, id: 977, filename: 'alt.pdf', title: 'Alter Vertrag' };
    let gone = false;
    mockedGet.mockImplementation((url: string) => {
      if (url === '/api/knowledge/documents/977') {
        return gone
          ? Promise.reject(Object.assign(new Error('gone'), { response: { status } }))
          : Promise.resolve(createMockResponse(FAR));
      }
      if (url.includes('/api/knowledge/documents')) return Promise.resolve(createMockResponse([DOC]));
      if (url.includes('/api/config/features')) {
        return Promise.resolve(createMockResponse({ schicht_a_extraction_enabled: true }));
      }
      return Promise.resolve(createMockResponse([]));
    });
    const queryClient = createTestQueryClient();
    renderWithProviders(<KnowledgePage />, { route: '/knowledge?doc=977', queryClient });
    await waitFor(() => expect(document.getElementById('doc-977')).not.toBeNull());

    // What a delete / move / tier mutation does on success.
    gone = true;
    await queryClient.invalidateQueries({ queryKey: ['knowledge'] });

    await waitFor(() => expect(document.getElementById('doc-977')).toBeNull());
    expect(document.getElementById('doc-5')).not.toBeNull();
  });

  it('does not pin the deep-linked document on top of a document search', async () => {
    const FAR = { ...DOC, id: 977, filename: 'alt.pdf', title: 'Alter Vertrag' };
    const OTHER = { ...DOC, id: 6, filename: 'strom.pdf', title: 'Stromrechnung' };
    const byId: string[] = [];
    mockedGet.mockImplementation((url: string, config?: { params?: Record<string, unknown> }) => {
      if (url === '/api/knowledge/documents/977') {
        byId.push(url);
        return Promise.resolve(createMockResponse(FAR));
      }
      if (url.includes('/api/knowledge/documents')) {
        const searching = url.includes('q=') || Boolean(config?.params?.q);
        return Promise.resolve(createMockResponse(searching ? [OTHER] : [DOC]));
      }
      if (url.includes('/api/config/features')) {
        return Promise.resolve(createMockResponse({ schicht_a_extraction_enabled: true }));
      }
      return Promise.resolve(createMockResponse([]));
    });
    renderWithProviders(<KnowledgePage />, { route: '/knowledge?doc=977' });
    await waitFor(() => expect(document.getElementById('doc-977')).not.toBeNull());

    const searchBox = document.querySelector('input.input.flex-1') as HTMLInputElement;
    fireEvent.change(searchBox, { target: { value: 'Strom' } });

    // The search result arrives (debounced); 977 does not match and is not shown.
    await waitFor(() => expect(document.getElementById('doc-6')).not.toBeNull());
    expect(document.getElementById('doc-977')).toBeNull();
    const rows = Array.from(document.querySelectorAll('[id^="doc-"]')).map((el) => el.id);
    expect(rows).toEqual(['doc-6']);
    // ... and no second by-id fetch was made for the search view.
    expect(byId).toHaveLength(1);
  });

  it('does not auto-expand without a deep link', async () => {
    renderWithProviders(<KnowledgePage />, { route: '/knowledge' });
    const toggle = await screen.findByRole('button', { name: /Fakten/ });
    expect(toggle).toHaveAttribute('aria-expanded', 'false');
  });

  it('shows the Paperless status icon for a filed document', async () => {
    renderWithProviders(<KnowledgePage />, { route: '/knowledge' });
    await screen.findByText('Steuerbescheid');
    // in_paperless=true → the "filed in Paperless" icon (aria-label)
    expect(screen.getByLabelText('In Paperless abgelegt')).toBeInTheDocument();
  });

  it('sorting by import date refetches with sort params', async () => {
    renderWithProviders(<KnowledgePage />, { route: '/knowledge' });
    await screen.findByText('Steuerbescheid');
    fireEvent.click(screen.getByRole('button', { name: /Importdatum/ }));
    await waitFor(() => {
      const sorted = mockedGet.mock.calls.some(
        ([url, cfg]: [string, { params?: Record<string, unknown> }?]) =>
          typeof url === 'string' &&
          url.includes('/api/knowledge/documents') &&
          cfg?.params?.sort === 'imported',
      );
      expect(sorted).toBe(true);
    });
  });

  it('reindex invalidates the document\'s facts cache (D5)', async () => {
    const queryClient = createTestQueryClient();
    const invalidateSpy = vi.spyOn(queryClient, 'invalidateQueries');
    renderWithProviders(<KnowledgePage />, { route: '/knowledge', queryClient });
    await screen.findByText('Steuerbescheid');
    fireEvent.click(screen.getByTitle('Neu indexieren'));
    await waitFor(() =>
      expect(invalidateSpy).toHaveBeenCalledWith({ queryKey: ['brain', 'facts', 5] }),
    );
  });
});
