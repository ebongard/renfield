/**
 * ObligationsPage — urgency grouping, empty state (Cormorant), and the
 * Bestätigen → toast → undo flow (eng-review D-FLOW-1 / D12). German default.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest';
import { screen, waitFor, fireEvent, within } from '@testing-library/react';
import ObligationsPage from '../../../../src/frontend/src/pages/ObligationsPage';
import { renderWithRouter, createMockResponse } from '../test-utils';
import apiClient from '../../../../src/frontend/src/utils/axios';
import type { DocumentFact } from '../../../../src/frontend/src/api/resources/brain';

vi.mock('../../../../src/frontend/src/utils/axios', () => ({
  default: {
    get: vi.fn(),
    // The Bestätigen flow now writes to the server ledger via useBestaetigt.
    post: vi.fn().mockResolvedValue({ data: { confirmed: true } }),
    delete: vi.fn().mockResolvedValue({ data: { confirmed: false } }),
    put: vi.fn().mockResolvedValue({ data: { calendar_name: 'family', available: [] } }),
  },
  extractApiError: (_e: unknown, fallback: string) => fallback,
  extractFieldErrors: () => ({}),
}));

const mockedGet = vi.mocked(apiClient.get);

// Local-midnight YYYY-MM-DD offset from today (matches the page's grouping).
function isoOffset(days: number): string {
  const d = new Date();
  d.setDate(d.getDate() + days);
  const m = String(d.getMonth() + 1).padStart(2, '0');
  const dd = String(d.getDate()).padStart(2, '0');
  return `${d.getFullYear()}-${m}-${dd}`;
}

function obligation(id: number, days: number, overrides: Partial<DocumentFact> = {}): DocumentFact {
  return {
    id, document_id: 100 + id, atom_id: null, category: 'obligation', kind: 'zahlung',
    value: 'Zahlung', normalized_value: null, excerpt: null,
    obligation_date: isoOffset(days), amount_value: 89.9, amount_currency: 'EUR',
    legal_gate: false, payment_method: null, confidence: null, source: 'deterministic',
    circle_tier: 0, ...overrides,
  };
}

function wire(facts: DocumentFact[]) {
  mockedGet.mockResolvedValue(createMockResponse(facts));
}

describe('ObligationsPage', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    mockedGet.mockReset();
    localStorage.clear();
    // jsdom doesn't implement scrollIntoView (used by the #frist- highlight).
    Element.prototype.scrollIntoView = vi.fn();
  });

  it('groups obligations into Überfällig / Diese Woche / Später', async () => {
    wire([obligation(1, -3), obligation(2, 2), obligation(3, 20)]);
    renderWithRouter(<ObligationsPage />, { route: '/brain/fristen' });
    expect(await screen.findByText('Überfällig')).toBeInTheDocument();
    expect(screen.getByText('Diese Woche')).toBeInTheDocument();
    expect(screen.getByText('Später')).toBeInTheDocument();
  });

  it('renders the Cormorant empty state when there are no obligations', async () => {
    wire([]);
    renderWithRouter(<ObligationsPage />, { route: '/brain/fristen' });
    expect(await screen.findByText('Keine offenen Pflichten.')).toBeInTheDocument();
  });

  it('exposes an .ics export link carrying the current horizon', async () => {
    wire([obligation(1, 2)]);
    renderWithRouter(<ObligationsPage />, { route: '/brain/fristen' });
    const link = await screen.findByTestId('export-ics-link');
    expect(link).toHaveAttribute('href', expect.stringContaining('/api/atoms/obligations/export.ics'));
    expect(link).toHaveAttribute('href', expect.stringContaining('due_before='));
  });

  it('calendar-sync selector: lists writable calendars and PUTs the choice', async () => {
    // Branch GET by URL: calendar-pref → pref shape; obligations → facts.
    mockedGet.mockImplementation((url: string) => {
      if (url.includes('calendar-pref')) {
        return Promise.resolve(createMockResponse({
          calendar_name: null,
          available: [{ name: 'family', label: 'Familie' }],
        }));
      }
      return Promise.resolve(createMockResponse([obligation(1, 5)]));
    });
    const putMock = vi.mocked(apiClient.put);
    renderWithRouter(<ObligationsPage />, { route: '/brain/fristen' });

    const select = await screen.findByLabelText('Kalender-Sync:');
    expect(within(select as HTMLSelectElement).getByRole('option', { name: 'Familie' })).toBeInTheDocument();
    expect(screen.getByRole('option', { name: 'Aus' })).toBeInTheDocument();

    fireEvent.change(select, { target: { value: 'family' } });
    await waitFor(() =>
      expect(putMock).toHaveBeenCalledWith('/api/atoms/obligations/calendar-pref', { calendar_name: 'family' }),
    );
  });

  it('calendar-sync selector hidden when no writable calendars (MCP off)', async () => {
    wire([obligation(1, 2)]); // calendar-pref GET also returns the facts array → available undefined → []
    renderWithRouter(<ObligationsPage />, { route: '/brain/fristen' });
    await screen.findByTestId('export-ics-link');
    expect(screen.queryByLabelText('Kalender-Sync:')).toBeNull();
  });

  it('confirm → toast appears; undo removes it', async () => {
    wire([obligation(1, 2)]);
    renderWithRouter(<ObligationsPage />, { route: '/brain/fristen' });

    const confirmBtn = await screen.findByRole('button', { name: /Bestätigen/ });
    fireEvent.click(confirmBtn);

    const toast = await screen.findByRole('status');
    expect(within(toast).getByText('Bestätigt')).toBeInTheDocument();

    fireEvent.click(within(toast).getByRole('button', { name: 'Rückgängig' }));
    await waitFor(() => expect(screen.queryByRole('status')).not.toBeInTheDocument());
  });

  it('does not show "Mehr laden" for a short list', async () => {
    wire([obligation(1, 2), obligation(2, 5)]);
    renderWithRouter(<ObligationsPage />, { route: '/brain/fristen' });
    await screen.findByText('Diese Woche');
    expect(screen.queryByRole('button', { name: 'Mehr laden' })).not.toBeInTheDocument();
  });

  it('"Mehr laden" appends the next offset page, then hides when exhausted (D9)', async () => {
    const page1 = Array.from({ length: 200 }, (_, i) => obligation(i + 1, 2));
    const page2 = [obligation(1000, 5)];
    mockedGet.mockImplementation((_url: string, cfg?: { params?: { offset?: number } }) => {
      const offset = cfg?.params?.offset ?? 0;
      return Promise.resolve(createMockResponse(offset === 0 ? page1 : page2));
    });
    const { container } = renderWithRouter(<ObligationsPage />, { route: '/brain/fristen' });
    const loadMore = await screen.findByRole('button', { name: 'Mehr laden' });
    fireEvent.click(loadMore);
    await waitFor(() => expect(container.querySelector('#frist-1000')).toBeInTheDocument());
    expect(screen.queryByRole('button', { name: 'Mehr laden' })).not.toBeInTheDocument();
  });

  it('highlights the row targeted by #frist-{id} (T6)', async () => {
    wire([obligation(1, 2), obligation(2, 5)]);
    const { container } = renderWithRouter(<ObligationsPage />, { route: '/brain/fristen#frist-2' });
    await waitFor(() =>
      expect(container.querySelector('#frist-2')?.className).toContain('animate-gentle-pulse'),
    );
    expect(container.querySelector('#frist-1')?.className).not.toContain('animate-gentle-pulse');
  });
});
