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
