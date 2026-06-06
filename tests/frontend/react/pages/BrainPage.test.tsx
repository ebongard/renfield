/**
 * BrainPage — document_fact render (T7).
 *
 * Verifies a Schicht A fact result surfaces in /brain search with the green
 * "Fakt" badge (German is the test default) and its snippet.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest';
import { screen, fireEvent, waitFor } from '@testing-library/react';
import type { AxiosResponse } from 'axios';
import BrainPage from '../../../../src/frontend/src/pages/BrainPage';
import { renderWithRouter } from '../test-utils';
import apiClient from '../../../../src/frontend/src/utils/axios';
import type { AtomMatch } from '../../../../src/frontend/src/api/resources/brain';

vi.mock('../../../../src/frontend/src/utils/axios', () => ({
  default: {
    get: vi.fn(),
  },
  extractApiError: (err: unknown, fallback: string): string => {
    const detail = (err as { response?: { data?: { detail?: unknown } } })?.response?.data?.detail;
    return typeof detail === 'string' ? detail : fallback;
  },
  extractFieldErrors: (): Record<string, string> => ({}),
}));

const mockedGet = vi.mocked(apiClient.get);

function mockSearch(matches: AtomMatch[]) {
  const response: AxiosResponse<AtomMatch[]> = {
    data: matches,
    status: 200,
    statusText: 'OK',
    headers: {},
    config: { headers: {} as never },
  };
  mockedGet.mockResolvedValueOnce(response);
}

describe('BrainPage document_fact rendering', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    mockedGet.mockReset();
  });

  it('renders a document_fact result with the green Fakt badge + snippet', async () => {
    mockSearch([
      {
        atom: { atom_id: 'fa-1', atom_type: 'document_fact', tier: 0 },
        score: 0.42,
        snippet: 'Finanzverwaltung NRW',
        rank: 1,
      },
    ]);

    renderWithRouter(<BrainPage />, { route: '/brain' });

    fireEvent.change(screen.getByRole('textbox'), { target: { value: 'Finanzamt' } });
    fireEvent.click(screen.getByRole('button', { name: 'Suchen' }));

    const badge = await waitFor(() => screen.getByText('Fakt'));
    expect(badge).toBeInTheDocument();
    expect(badge.className).toContain('green');
    expect(screen.getByText('Finanzverwaltung NRW')).toBeInTheDocument();
  });

  it('Fakten filter chip narrows mixed results to document_fact only', async () => {
    mockSearch([
      { atom: { atom_id: 'fa-1', atom_type: 'document_fact', tier: 0 }, score: 0.9, snippet: 'Finanzverwaltung NRW', rank: 1 },
      { atom: { atom_id: 'kg-1', atom_type: 'kg_node', tier: 0 }, score: 0.8, snippet: 'Müller GmbH', rank: 2 },
    ]);

    renderWithRouter(<BrainPage />, { route: '/brain' });
    fireEvent.change(screen.getByRole('textbox'), { target: { value: 'x' } });
    fireEvent.click(screen.getByRole('button', { name: 'Suchen' }));

    await waitFor(() => screen.getByText('Finanzverwaltung NRW'));
    expect(screen.getByText('Müller GmbH')).toBeInTheDocument();

    // narrow to facts only → the kg_node row disappears
    fireEvent.click(screen.getByRole('button', { name: /Nur Fakten/ }));
    expect(screen.getByText('Finanzverwaltung NRW')).toBeInTheDocument();
    expect(screen.queryByText('Müller GmbH')).toBeNull();

    // back to All restores it
    fireEvent.click(screen.getByRole('button', { name: /Alle/ }));
    expect(screen.getByText('Müller GmbH')).toBeInTheDocument();
  });
});
