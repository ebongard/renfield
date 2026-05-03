/**
 * F4d — federation audit page.
 *
 * Covers: empty state, happy-path render with multiple entries, row
 * expansion, peer filter via query param, and the success/failed/unknown
 * status icons + localized copy.
 */
import { describe, it, expect, vi, beforeEach, beforeAll } from 'vitest';
import { screen, fireEvent, waitFor } from '@testing-library/react';
import type { AxiosResponse } from 'axios';
import FederationAuditPage from '../../../../src/frontend/src/pages/FederationAuditPage';
import { renderWithRouter } from '../test-utils';
import apiClient from '../../../../src/frontend/src/utils/axios';
import type { AuditEntry } from '../../../../src/frontend/src/api/resources/federation';

interface AuditResponseBody {
  entries: AuditEntry[];
  limit: number;
  offset: number;
  peer_pubkey: string | null;
}

vi.mock('../../../../src/frontend/src/utils/axios', () => ({
  default: {
    get: vi.fn(),
  },
  extractApiError: (err: unknown, fallback: string): string => {
    const detail = (err as { response?: { data?: { detail?: unknown } } })?.response?.data?.detail;
    if (!detail) return fallback;
    if (typeof detail === 'string') return detail;
    return fallback;
  },
  extractFieldErrors: (): Record<string, string> => ({}),
}));

beforeAll(() => {
  // In case any child component tries scrollIntoView
  Element.prototype.scrollIntoView = vi.fn();
});

const MOM_PUBKEY = 'm'.repeat(64);
const DAD_PUBKEY = 'd'.repeat(64);

// Helper: typed handle on the mocked apiClient.get
const mockedGet = vi.mocked(apiClient.get);

function mockAuditResponse(body: AuditResponseBody) {
  const response: AxiosResponse<AuditResponseBody> = {
    data: body,
    status: 200,
    statusText: 'OK',
    headers: {},
    // axios v1 requires a config object — use a permissive typed empty cast
    // through the AxiosResponse generic (no `as any`).
    config: { headers: {} as never },
  };
  mockedGet.mockResolvedValueOnce(response);
}

describe('FederationAuditPage', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    mockedGet.mockReset();
  });

  it('renders empty state when no entries', async () => {
    mockAuditResponse({ entries: [], limit: 50, offset: 0, peer_pubkey: null });

    renderWithRouter(<FederationAuditPage />, { route: '/brain/audit' });

    await waitFor(() => {
      expect(screen.getByText(/Noch keine föderierten Anfragen/i)).toBeInTheDocument();
    });
  });

  it('renders rows with peer, query, and initiation time', async () => {
    mockAuditResponse({
      entries: [
        {
          id: '1',
          peer_pubkey: MOM_PUBKEY,
          peer_display_name: 'Mom',
          query_text: 'Wann ist Omas Geburtstag?',
          initiated_at: '2026-04-22T10:15:00',
          finalized_at: '2026-04-22T10:15:03',
          final_status: 'success',
          verified_signature: true,
          answer_excerpt: 'Am 14. Juni.',
        },
        {
          id: '2',
          peer_pubkey: DAD_PUBKEY,
          peer_display_name: 'Dad',
          query_text: 'Wie spät ist es bei dir?',
          initiated_at: '2026-04-22T09:00:00',
          final_status: 'failed',
          verified_signature: false,
          error_message: 'Peer connection refused',
        },
      ],
      limit: 50,
      offset: 0,
      peer_pubkey: null,
    });

    renderWithRouter(<FederationAuditPage />, { route: '/brain/audit' });

    await waitFor(() => {
      expect(screen.getByText('Mom')).toBeInTheDocument();
      expect(screen.getByText('Dad')).toBeInTheDocument();
    });
    expect(screen.getByText('Wann ist Omas Geburtstag?')).toBeInTheDocument();
    expect(screen.getByText('Wie spät ist es bei dir?')).toBeInTheDocument();
  });

  it('expanding a row shows the full answer and fingerprint', async () => {
    mockAuditResponse({
      entries: [
        {
          id: '1',
          peer_pubkey: MOM_PUBKEY,
          peer_display_name: 'Mom',
          query_text: 'Wann ist Omas Geburtstag?',
          initiated_at: '2026-04-22T10:15:00',
          finalized_at: '2026-04-22T10:15:03',
          final_status: 'success',
          verified_signature: true,
          answer_excerpt: 'Am 14. Juni. Sie wird 87.',
        },
      ],
      limit: 50,
      offset: 0,
      peer_pubkey: null,
    });

    renderWithRouter(<FederationAuditPage />, { route: '/brain/audit' });

    const row = await screen.findByRole('button', { expanded: false, name: /Mom/i });
    fireEvent.click(row);

    await waitFor(() => {
      expect(screen.getByText('Am 14. Juni. Sie wird 87.')).toBeInTheDocument();
      expect(screen.getByText(new RegExp(MOM_PUBKEY.slice(0, 12)))).toBeInTheDocument();
    });
  });

  it('renders the error message on a failed entry when expanded', async () => {
    mockAuditResponse({
      entries: [
        {
          id: '99',
          peer_pubkey: DAD_PUBKEY,
          peer_display_name: 'Dad',
          query_text: 'Was gibt es zum Abendessen?',
          initiated_at: '2026-04-22T18:00:00',
          finalized_at: '2026-04-22T18:00:05',
          final_status: 'failed',
          verified_signature: false,
          error_message: 'Responder signature verification failed',
        },
      ],
      limit: 50,
      offset: 0,
      peer_pubkey: null,
    });

    renderWithRouter(<FederationAuditPage />, { route: '/brain/audit' });

    const row = await screen.findByRole('button', { name: /Dad/i });
    fireEvent.click(row);

    await waitFor(() => {
      expect(screen.getByText('Responder signature verification failed')).toBeInTheDocument();
    });
  });

  it('filters by peer when ?peer= query param is present', async () => {
    mockAuditResponse({
      entries: [
        {
          id: '1',
          peer_pubkey: MOM_PUBKEY,
          peer_display_name: 'Mom',
          query_text: 'Wann war die Hochzeit?',
          initiated_at: '2026-04-22T10:00:00',
          finalized_at: '2026-04-22T10:00:02',
          final_status: 'success',
          verified_signature: true,
          answer_excerpt: 'Am 3. Juli.',
        },
      ],
      limit: 50,
      offset: 0,
      peer_pubkey: MOM_PUBKEY,
    });

    renderWithRouter(<FederationAuditPage />, { route: `/brain/audit?peer=${MOM_PUBKEY}` });

    await waitFor(() => {
      expect(mockedGet).toHaveBeenCalledWith(
        expect.stringContaining(`peer_pubkey=${MOM_PUBKEY}`),
      );
      expect(screen.getByText(/Gefiltert auf Mom/i)).toBeInTheDocument();
    });
  });

  it('shows error alert on API failure', async () => {
    mockedGet.mockRejectedValueOnce(new Error('500'));

    renderWithRouter(<FederationAuditPage />, { route: '/brain/audit' });

    await waitFor(() => {
      expect(screen.getByText(/Verlauf konnte nicht geladen werden/i)).toBeInTheDocument();
    });
  });
});
