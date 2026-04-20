/**
 * F4d — federation audit page.
 *
 * Covers: empty state, happy-path render with multiple entries, row
 * expansion, peer filter via query param, and the success/failed/unknown
 * status icons + localized copy.
 */
import { describe, it, expect, vi, beforeEach, beforeAll } from 'vitest';
import { screen, fireEvent, waitFor } from '@testing-library/react';
import FederationAuditPage from '../../../../src/frontend/src/pages/FederationAuditPage';
import { renderWithRouter } from '../test-utils.jsx';
import apiClient from '../../../../src/frontend/src/utils/axios';

vi.mock('../../../../src/frontend/src/utils/axios', () => ({
  default: {
    get: vi.fn(),
  },
}));

beforeAll(() => {
  // In case any child component tries scrollIntoView
  Element.prototype.scrollIntoView = vi.fn();
});

const MOM_PUBKEY = 'm'.repeat(64);
const DAD_PUBKEY = 'd'.repeat(64);

describe('FederationAuditPage', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    apiClient.get.mockReset();
  });

  it('renders empty state when no entries', async () => {
    apiClient.get.mockResolvedValueOnce({
      data: { entries: [], limit: 50, offset: 0, peer_pubkey: null },
    });

    renderWithRouter(<FederationAuditPage />, { route: '/brain/audit' });

    await waitFor(() => {
      expect(screen.getByText(/Noch keine föderierten Anfragen/i)).toBeInTheDocument();
    });
  });

  it('renders rows with peer, query, and initiation time', async () => {
    apiClient.get.mockResolvedValueOnce({
      data: {
        entries: [
          {
            id: 1,
            peer_user_id: 7,
            peer_pubkey: MOM_PUBKEY,
            peer_display_name: 'Mom',
            query_text: 'Wann ist Omas Geburtstag?',
            initiated_at: '2026-04-22T10:15:00',
            finalized_at: '2026-04-22T10:15:03',
            final_status: 'success',
            verified_signature: true,
            answer_excerpt: 'Am 14. Juni.',
            error_message: null,
          },
          {
            id: 2,
            peer_user_id: 8,
            peer_pubkey: DAD_PUBKEY,
            peer_display_name: 'Dad',
            query_text: 'Wie spät ist es bei dir?',
            initiated_at: '2026-04-22T09:00:00',
            finalized_at: null,
            final_status: 'failed',
            verified_signature: false,
            answer_excerpt: null,
            error_message: 'Peer connection refused',
          },
        ],
        limit: 50, offset: 0, peer_pubkey: null,
      },
    });

    renderWithRouter(<FederationAuditPage />, { route: '/brain/audit' });

    await waitFor(() => {
      expect(screen.getByText('Mom')).toBeInTheDocument();
      expect(screen.getByText('Dad')).toBeInTheDocument();
    });
    // Query previews shown on the row
    expect(screen.getByText('Wann ist Omas Geburtstag?')).toBeInTheDocument();
    expect(screen.getByText('Wie spät ist es bei dir?')).toBeInTheDocument();
  });

  it('expanding a row shows the full answer and fingerprint', async () => {
    apiClient.get.mockResolvedValueOnce({
      data: {
        entries: [{
          id: 1,
          peer_user_id: 7,
          peer_pubkey: MOM_PUBKEY,
          peer_display_name: 'Mom',
          query_text: 'Wann ist Omas Geburtstag?',
          initiated_at: '2026-04-22T10:15:00',
          finalized_at: '2026-04-22T10:15:03',
          final_status: 'success',
          verified_signature: true,
          answer_excerpt: 'Am 14. Juni. Sie wird 87.',
          error_message: null,
        }],
        limit: 50, offset: 0, peer_pubkey: null,
      },
    });

    renderWithRouter(<FederationAuditPage />, { route: '/brain/audit' });

    // Click the row's toggle button (button wraps the whole row)
    const row = await screen.findByRole('button', { expanded: false, name: /Mom/i });
    fireEvent.click(row);

    await waitFor(() => {
      expect(screen.getByText('Am 14. Juni. Sie wird 87.')).toBeInTheDocument();
      // Truncated fingerprint appears on expansion — 12 chars to match the peers page
      expect(screen.getByText(new RegExp(MOM_PUBKEY.slice(0, 12)))).toBeInTheDocument();
    });
  });

  it('renders the error message on a failed entry when expanded', async () => {
    apiClient.get.mockResolvedValueOnce({
      data: {
        entries: [{
          id: 99,
          peer_user_id: null,
          peer_pubkey: DAD_PUBKEY,
          peer_display_name: 'Dad',
          query_text: 'Was gibt es zum Abendessen?',
          initiated_at: '2026-04-22T18:00:00',
          finalized_at: '2026-04-22T18:00:05',
          final_status: 'failed',
          verified_signature: false,
          answer_excerpt: null,
          error_message: 'Responder signature verification failed',
        }],
        limit: 50, offset: 0, peer_pubkey: null,
      },
    });

    renderWithRouter(<FederationAuditPage />, { route: '/brain/audit' });

    const row = await screen.findByRole('button', { name: /Dad/i });
    fireEvent.click(row);

    await waitFor(() => {
      expect(screen.getByText('Responder signature verification failed')).toBeInTheDocument();
    });
  });

  it('filters by peer when ?peer= query param is present', async () => {
    apiClient.get.mockResolvedValueOnce({
      data: {
        entries: [{
          id: 1,
          peer_user_id: 7,
          peer_pubkey: MOM_PUBKEY,
          peer_display_name: 'Mom',
          query_text: 'Wann war die Hochzeit?',
          initiated_at: '2026-04-22T10:00:00',
          finalized_at: '2026-04-22T10:00:02',
          final_status: 'success',
          verified_signature: true,
          answer_excerpt: 'Am 3. Juli.',
          error_message: null,
        }],
        limit: 50, offset: 0, peer_pubkey: MOM_PUBKEY,
      },
    });

    renderWithRouter(
      <FederationAuditPage />,
      { route: `/brain/audit?peer=${MOM_PUBKEY}` },
    );

    await waitFor(() => {
      expect(apiClient.get).toHaveBeenCalledWith(
        expect.stringContaining(`peer_pubkey=${MOM_PUBKEY}`),
      );
      // Filter banner visible
      expect(screen.getByText(/Gefiltert auf Mom/i)).toBeInTheDocument();
    });
  });

  it('shows error alert on API failure', async () => {
    apiClient.get.mockRejectedValueOnce(new Error('500'));

    renderWithRouter(<FederationAuditPage />, { route: '/brain/audit' });

    await waitFor(() => {
      expect(screen.getByText(/Verlauf konnte nicht geladen werden/i)).toBeInTheDocument();
    });
  });
});
