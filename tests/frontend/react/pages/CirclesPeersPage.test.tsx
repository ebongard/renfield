/**
 * /settings/circles/peers -- the paired instances.
 *
 * Two response shapes reach this page:
 *   - the legacy one (the caller's own pairings, seven keys), and
 *   - the extended one behind FEDERATION_PEERS_VISIBLE_TO_ALL, where every
 *     account sees all active pairings plus `owned_by_me` / `paired_by` /
 *     `linked` / `can_revoke`.
 * The legacy shape must keep rendering as an own, revocable pairing.
 */
import { describe, it, expect, vi, beforeEach, beforeAll } from 'vitest';
import { screen, fireEvent, waitFor, within } from '@testing-library/react';
import type { AxiosResponse } from 'axios';
import CirclesPeersPage from '../../../../src/frontend/src/pages/CirclesPeersPage';
import { renderWithRouter } from '../test-utils';
import i18n from '../../../../src/frontend/src/i18n';
import apiClient from '../../../../src/frontend/src/utils/axios';
import type { FederationPeer, IdentityLink } from '../../../../src/frontend/src/api/resources/federation';

vi.mock('../../../../src/frontend/src/utils/axios', () => ({
  default: {
    get: vi.fn(),
    delete: vi.fn(),
    post: vi.fn(),
  },
  extractApiError: (_err: unknown, fallback: string): string => fallback,
  extractFieldErrors: (): Record<string, string> => ({}),
}));

beforeAll(() => {
  Element.prototype.scrollIntoView = vi.fn();
});

const mockedGet = vi.mocked(apiClient.get);
const mockedDelete = vi.mocked(apiClient.delete);

function response<T>(data: T): AxiosResponse<T> {
  return { data, status: 200, statusText: 'OK', headers: {}, config: { headers: {} as never } };
}

function serve(peers: FederationPeer[], links: IdentityLink[] = []) {
  mockedGet.mockImplementation((url: string) => {
    if (url === '/api/federation/peers') return Promise.resolve(response({ peers }));
    if (url === '/api/federation/links/mine') return Promise.resolve(response(links));
    return Promise.reject(new Error(`unexpected GET ${url}`));
  });
}

const PUBKEY = 'ab'.repeat(32);

const daysAgo = (n: number): string =>
  new Date(Date.now() - n * 24 * 60 * 60 * 1000 - 60_000).toISOString().replace('Z', '');

/** The legacy shape: exactly what a flag-off backend sends. */
const legacyPeer: FederationPeer = {
  id: '1',
  remote_display_name: 'Far Instance',
  remote_pubkey: PUBKEY,
  circle_tier: 2,
  last_seen_at: daysAgo(15),
};

/** Someone else's pairing, as a plain account sees it with the flag on. */
const foreignPeer: FederationPeer = {
  ...legacyPeer,
  owned_by_me: false,
  paired_by: 'Olga Owner',
  linked: false,
  can_revoke: false,
};

const tierName = (): string => i18n.t('circles.tier.2');

describe('CirclesPeersPage', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    mockedGet.mockReset();
    mockedDelete.mockReset();
  });

  describe('legacy response (flag off)', () => {
    it('renders an own, revocable pairing without any of the new lines', async () => {
      serve([legacyPeer]);
      renderWithRouter(<CirclesPeersPage />, { route: '/settings/circles/peers' });

      expect(await screen.findByText('Far Instance')).toBeInTheDocument();
      expect(screen.getByText(`${PUBKEY.slice(0, 12)}…`)).toBeInTheDocument();
      expect(screen.getByRole('button', { name: i18n.t('circles.revokePeer') })).toBeInTheDocument();
      expect(screen.getByRole('button', { name: i18n.t('circles.linkStartHandshake') })).toBeInTheDocument();
      expect(screen.queryByTestId('peer-ask-hint-1')).not.toBeInTheDocument();
      expect(screen.queryByTestId('peer-grant-hint-1')).not.toBeInTheDocument();
      expect(screen.queryByText(/Olga Owner/)).not.toBeInTheDocument();
    });

    it('labels the time as the last REQUEST, not as a connection state', async () => {
      serve([legacyPeer]);
      renderWithRouter(<CirclesPeersPage />, { route: '/settings/circles/peers' });

      const expected = i18n.t('circles.peerLastRequest', {
        when: i18n.t('circles.peerDaysAgo', { n: 15 }),
      });
      const label = await screen.findByText(expected);
      expect(label).toHaveAttribute('title', i18n.t('circles.peerLastRequestHelp'));
    });

    it('says "never asked" for a peer that has not sent a request yet', async () => {
      serve([{ ...legacyPeer, last_seen_at: null }]);
      renderWithRouter(<CirclesPeersPage />, { route: '/settings/circles/peers' });

      expect(await screen.findByText(i18n.t('circles.peerNeverAsked'))).toBeInTheDocument();
    });
  });

  describe('extended response (flag on)', () => {
    it("shows someone else's pairing with its owner and without a revoke button", async () => {
      serve([foreignPeer]);
      renderWithRouter(<CirclesPeersPage />, { route: '/settings/circles/peers' });

      expect(await screen.findByText('Far Instance')).toBeInTheDocument();
      expect(
        screen.getByText(i18n.t('circles.peerPairedBy', { name: 'Olga Owner' })),
      ).toBeInTheDocument();
      expect(screen.queryByRole('button', { name: i18n.t('circles.revokePeer') })).not.toBeInTheDocument();
      // The person handshake needs a pairing the person owns.
      expect(
        screen.queryByRole('button', { name: i18n.t('circles.linkStartHandshake') }),
      ).not.toBeInTheDocument();
    });

    it('tells an unlinked account that the far side decides what it receives', async () => {
      serve([foreignPeer]);
      renderWithRouter(<CirclesPeersPage />, { route: '/settings/circles/peers' });

      expect(await screen.findByTestId('peer-ask-hint-1')).toHaveTextContent(
        i18n.t('circles.peerAskUnlinked', { name: 'Far Instance' }),
      );
    });

    it('tells a linked account that it asks as itself', async () => {
      serve([{ ...foreignPeer, linked: true }]);
      renderWithRouter(<CirclesPeersPage />, { route: '/settings/circles/peers' });

      expect(await screen.findByTestId('peer-ask-hint-1')).toHaveTextContent(
        i18n.t('circles.peerAskLinked', { name: 'Far Instance' }),
      );
    });

    it("names the owner and the tier in what the far side receives from here", async () => {
      serve([foreignPeer]);
      renderWithRouter(<CirclesPeersPage />, { route: '/settings/circles/peers' });

      const hint = await screen.findByTestId('peer-grant-hint-1');
      expect(hint).toHaveTextContent(
        i18n.t('circles.peerGrantOther', {
          name: 'Far Instance', pairedBy: 'Olga Owner', tier: tierName(),
        }),
      );
      expect(hint).toHaveTextContent('Olga Owner');
      expect(hint).toHaveTextContent(tierName());
    });

    it('speaks of "you" on an own pairing and keeps both buttons', async () => {
      serve([{ ...foreignPeer, owned_by_me: true, can_revoke: true }]);
      renderWithRouter(<CirclesPeersPage />, { route: '/settings/circles/peers' });

      expect(await screen.findByTestId('peer-grant-hint-1')).toHaveTextContent(
        i18n.t('circles.peerGrantMine', { name: 'Far Instance', tier: tierName() }),
      );
      expect(
        screen.queryByText(i18n.t('circles.peerPairedBy', { name: 'Olga Owner' })),
      ).not.toBeInTheDocument();
      expect(screen.getByRole('button', { name: i18n.t('circles.revokePeer') })).toBeInTheDocument();
      expect(screen.getByRole('button', { name: i18n.t('circles.linkStartHandshake') })).toBeInTheDocument();
    });

    it("lets an admin revoke someone else's pairing, with a text that fits", async () => {
      serve([{ ...foreignPeer, can_revoke: true }]);
      mockedDelete.mockResolvedValue(response(undefined));
      renderWithRouter(<CirclesPeersPage />, { route: '/settings/circles/peers' });

      fireEvent.click(await screen.findByRole('button', { name: i18n.t('circles.revokePeer') }));

      const dialog = await screen.findByRole('dialog');
      expect(dialog).toHaveTextContent(
        i18n.t('circles.revokePeerConfirmOther', { name: 'Far Instance', pairedBy: 'Olga Owner' }),
      );
      fireEvent.click(within(dialog).getByRole('button', { name: i18n.t('common.delete') }));

      await waitFor(() => {
        expect(mockedDelete).toHaveBeenCalledWith('/api/federation/peers/1');
      });
    });
  });

  it('has every new key in all four locales', () => {
    const keys = [
      'peerNeverAsked', 'peerLastRequest', 'peerLastRequestHelp', 'peerJustNow',
      'peerPairedBy', 'peerAskLinked', 'peerAskUnlinked', 'peerGrantMine',
      'peerGrantOther', 'revokePeerConfirmOther',
    ];
    for (const lng of ['de', 'en', 'fr', 'it']) {
      for (const key of keys) {
        expect(i18n.exists(`circles.${key}`, { lng, fallbackLng: [] }), `${lng}: ${key}`).toBe(true);
      }
    }
  });
});
