/**
 * /settings/circles/peers -- the paired instances.
 *
 * Two response shapes reach this page:
 *   - the legacy one (the caller's own pairings, seven keys), and
 *   - the extended one behind FEDERATION_PEERS_VISIBLE_TO_ALL, where every
 *     account sees all active pairings plus `owned_by_me` / `paired_by` /
 *     `linked` / `can_revoke`.
 * The legacy shape must keep rendering as an own, revocable pairing.
 *
 * Two further additive groups, each absent unless the backend has something to
 * say: `instance_name` (the far instance's label) and, behind
 * FEDERATION_REACHABILITY_ENABLED, `reachability` / `last_asked_at` /
 * `recently_active`.
 */
import { describe, it, expect, vi, beforeEach, beforeAll } from 'vitest';
import { act, screen, fireEvent, waitFor, within } from '@testing-library/react';
import type { AxiosResponse } from 'axios';
import CirclesPeersPage from '../../../../src/frontend/src/pages/CirclesPeersPage';
import { createTestQueryClient, renderWithRouter } from '../test-utils';
import { keys } from '../../../../src/frontend/src/api/keys';
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
const mockedPost = vi.mocked(apiClient.post);

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

const minutesAgo = (n: number): string =>
  new Date(Date.now() - n * 60_000 - 5_000).toISOString().replace('Z', '');

/** The legacy shape: exactly what a flag-off backend sends. */
const legacyPeer: FederationPeer = {
  id: 1,
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
    mockedPost.mockReset();
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

  describe('the instance name', () => {
    it('shows the instance first and the person second', async () => {
      serve([{ ...legacyPeer, remote_display_name: 'alex', instance_name: 'Instance B' }]);
      renderWithRouter(<CirclesPeersPage />, { route: '/settings/circles/peers' });

      expect(await screen.findByText('Instance B')).toBeInTheDocument();
      expect(screen.getByTestId('peer-person-1')).toHaveTextContent('· alex');
    });

    it.each([undefined, null, '', '   '])(
      'shows the person alone when there is no instance label (%j)',
      async (instanceName) => {
        serve([{ ...legacyPeer, instance_name: instanceName }]);
        renderWithRouter(<CirclesPeersPage />, { route: '/settings/circles/peers' });

        expect(await screen.findByText('Far Instance')).toBeInTheDocument();
        expect(screen.queryByTestId('peer-person-1')).not.toBeInTheDocument();
      },
    );

    it('calls the peer by its instance name in the hints', async () => {
      serve([{ ...foreignPeer, remote_display_name: 'alex', instance_name: 'Instance B' }]);
      renderWithRouter(<CirclesPeersPage />, { route: '/settings/circles/peers' });

      expect(await screen.findByTestId('peer-ask-hint-1')).toHaveTextContent(
        i18n.t('circles.peerAskUnlinked', { name: 'Instance B' }),
      );
      expect(screen.getByTestId('peer-grant-hint-1')).toHaveTextContent(
        i18n.t('circles.peerGrantOther', {
          name: 'Instance B', pairedBy: 'Olga Owner', tier: tierName(),
        }),
      );
    });

    it('names instance AND person where a pairing is revoked', async () => {
      serve([{ ...legacyPeer, remote_display_name: 'alex', instance_name: 'Instance B' }]);
      mockedDelete.mockResolvedValue(response(undefined));
      renderWithRouter(<CirclesPeersPage />, { route: '/settings/circles/peers' });

      fireEvent.click(await screen.findByRole('button', { name: i18n.t('circles.revokePeer') }));
      const dialog = await screen.findByRole('dialog');
      expect(dialog).toHaveTextContent(
        i18n.t('circles.revokePeerConfirm', { name: 'Instance B · alex' }),
      );
    });

    it('labels a person link with the instance too', async () => {
      serve([legacyPeer], [
        { id: 7, peer_id: 1, peer_display_name: 'alex', peer_instance_name: 'Instance B', created_at: daysAgo(2) },
        { id: 8, peer_id: 2, peer_display_name: 'robin', created_at: daysAgo(2) },
      ]);
      renderWithRouter(<CirclesPeersPage />, { route: '/settings/circles/peers' });

      expect(await screen.findByText('Instance B · alex')).toBeInTheDocument();
      expect(screen.getByText('robin')).toBeInTheDocument();
    });
  });

  describe('reachability (FEDERATION_REACHABILITY_ENABLED)', () => {
    const reachable: FederationPeer = {
      ...legacyPeer,
      reachability: { state: 'reachable', reason: null, checked_at: minutesAgo(3) },
      last_asked_at: null,
      recently_active: false,
    };
    // The button's accessible name is its VISIBLE text followed by the peer
    // it checks (a page lists several); what is visible stays the short
    // "check now".
    const checkNow = (peer = 'Far Instance') =>
      screen.getByRole('button', { name: `${i18n.t('circles.peerReachCheckNow')} ${peer}` });

    it('says nothing about reachability when the backend sends none', async () => {
      serve([legacyPeer]);
      renderWithRouter(<CirclesPeersPage />, { route: '/settings/circles/peers' });

      await screen.findByText('Far Instance');
      expect(screen.queryByTestId('peer-reach-1')).not.toBeInTheDocument();
      expect(
        screen.queryByRole('button', { name: i18n.t('circles.peerReachCheckNow') }),
      ).not.toBeInTheDocument();
      expect(screen.queryByTestId('peer-recent-1')).not.toBeInTheDocument();
    });

    it('shows the state and when it was checked -- and keeps the last-request line', async () => {
      serve([reachable]);
      renderWithRouter(<CirclesPeersPage />, { route: '/settings/circles/peers' });

      const line = await screen.findByTestId('peer-reach-1');
      expect(line).toHaveAttribute('data-reach-state', 'reachable');
      expect(line).toHaveTextContent(i18n.t('circles.peerReachState.reachable'));
      expect(screen.getByTestId('peer-reach-checked-1')).toHaveTextContent(
        i18n.t('circles.peerReachCheckedAt', { when: i18n.t('circles.peerMinutesAgo', { n: 3 }) }),
      );
      // Two different facts, two lines: the last REQUEST stays what it was.
      expect(
        screen.getByText(i18n.t('circles.peerLastRequest', {
          when: i18n.t('circles.peerDaysAgo', { n: 15 }),
        })),
      ).toBeInTheDocument();
    });

    it.each([
      'unreachable', 'pin_mismatch', 'refused', 'invalid_response', 'no_endpoint',
    ])('explains an unreachable peer in one line (%s)', async (reason) => {
      serve([{ ...reachable, reachability: { state: 'unreachable', reason, checked_at: minutesAgo(3) } }]);
      renderWithRouter(<CirclesPeersPage />, { route: '/settings/circles/peers' });

      const line = await screen.findByTestId('peer-reach-1');
      expect(line).toHaveAttribute('data-reach-state', 'unreachable');
      expect(line).toHaveTextContent(i18n.t('circles.peerReachState.unreachable'));
      expect(line).toHaveTextContent(i18n.t(`circles.peerReachReason.${reason}`));
    });

    it('shows a far side that cannot answer a check as UNKNOWN, not as unreachable', async () => {
      serve([{
        ...reachable,
        reachability: { state: 'unknown', reason: 'unsupported', checked_at: minutesAgo(3) },
      }]);
      renderWithRouter(<CirclesPeersPage />, { route: '/settings/circles/peers' });

      const line = await screen.findByTestId('peer-reach-1');
      expect(line).toHaveAttribute('data-reach-state', 'unknown');
      expect(line).toHaveTextContent(i18n.t('circles.peerReachState.unknown'));
      expect(line).toHaveTextContent(i18n.t('circles.peerReachReason.unsupported'));
      expect(line).not.toHaveTextContent(i18n.t('circles.peerReachState.unreachable'));
    });

    const unchecked: FederationPeer = {
      ...legacyPeer,
      reachability: { state: 'unknown', reason: 'not_checked', checked_at: null },
      last_asked_at: null,
      recently_active: false,
    };

    it('shows "not checked yet" without a check time', async () => {
      serve([unchecked]);
      // The check the page starts by itself comes back empty-handed (the
      // server's per-pairing floor): the row stays what it was.
      mockedPost.mockResolvedValue(response(unchecked.reachability));
      renderWithRouter(<CirclesPeersPage />, { route: '/settings/circles/peers' });

      const line = await screen.findByTestId('peer-reach-1');
      expect(line).toHaveTextContent(i18n.t('circles.peerReachReason.not_checked'));
      expect(screen.queryByTestId('peer-reach-checked-1')).not.toBeInTheDocument();
    });

    it('checks a row nobody has checked ONCE when the page shows it', async () => {
      // Without a connected wall display nothing keeps the readings current,
      // so the page would otherwise open on "not checked" for every row.
      serve([unchecked, { ...reachable, id: 2 }]);
      mockedPost.mockResolvedValue(
        response({ state: 'reachable', reason: null, checked_at: minutesAgo(0) }),
      );
      renderWithRouter(<CirclesPeersPage />, { route: '/settings/circles/peers' });

      await waitFor(() => {
        expect(screen.getByTestId('peer-reach-1')).toHaveAttribute('data-reach-state', 'reachable');
      });
      // Only the unchecked row, and only once -- the row with a reading is
      // left to its "check now" button.
      expect(mockedPost.mock.calls).toEqual([['/api/federation/peers/1/check']]);
    });

    it('does not ask again when that first check comes back without a reading', async () => {
      serve([unchecked]);
      mockedPost.mockResolvedValue(response(unchecked.reachability));
      renderWithRouter(<CirclesPeersPage />, { route: '/settings/circles/peers' });

      await waitFor(() => expect(mockedPost).toHaveBeenCalledTimes(1));
      await waitFor(() => expect(checkNow()).not.toBeDisabled());
      expect(mockedPost).toHaveBeenCalledTimes(1);
    });

    it('stays quiet when the check it started by itself fails -- but leaves a debug line', async () => {
      const debug = vi.spyOn(console, 'debug').mockImplementation(() => {});
      serve([unchecked]);
      const boom = new Error('boom');
      mockedPost.mockRejectedValue(boom);
      renderWithRouter(<CirclesPeersPage />, { route: '/settings/circles/peers' });

      await waitFor(() => expect(mockedPost).toHaveBeenCalledTimes(1));
      await waitFor(() => expect(checkNow()).not.toBeDisabled());
      expect(screen.queryByText(i18n.t('circles.peerReachCheckFailed'))).not.toBeInTheDocument();
      expect(screen.getByTestId('peer-reach-1')).toHaveAttribute('data-reach-state', 'unknown');
      // Silent to the person; whoever debugs it finds the row id and the error.
      expect(debug).toHaveBeenCalledTimes(1);
      expect(debug.mock.calls[0]).toEqual([expect.stringContaining('reachability check failed'), 1, boom]);
      debug.mockRestore();
    });

    it('names the peer on each "check now" button and announces the state politely', async () => {
      serve([
        { ...reachable, instance_name: 'Instance B' },
        { ...reachable, id: 2, remote_display_name: 'Someone Else' },
      ]);
      renderWithRouter(<CirclesPeersPage />, { route: '/settings/circles/peers' });

      await screen.findByTestId('peer-reach-1');
      // Two rows, two buttons with the same visible text -- told apart by name.
      expect(checkNow('Instance B · Far Instance')).toHaveTextContent(i18n.t('circles.peerReachCheckNow'));
      expect(checkNow('Someone Else')).toBeInTheDocument();

      const status = screen.getByTestId('peer-reach-status-1');
      expect(status).toHaveAttribute('aria-live', 'polite');
      // State and reason are read as ONE statement, not as two fragments.
      expect(status).toHaveAttribute('aria-atomic', 'true');
      // A real box: `display: contents` drops an element from the
      // accessibility tree in some browsers, and the live region with it.
      expect(status.className.split(/\s+/)).not.toContain('contents');
      expect(status).toHaveTextContent(i18n.t('circles.peerReachState.reachable'));
      // The button is NOT inside the live region: its "checking…" is not news.
      expect(within(status).queryByRole('button')).not.toBeInTheDocument();
      // Neither is the relative time: "3 minutes ago" becoming "4 minutes ago"
      // would be announced on every re-render.
      const checked = screen.getAllByTestId('peer-reach-checked-1');
      expect(checked).toHaveLength(1);
      expect(status).not.toContainElement(checked[0]);
      expect(status).not.toHaveTextContent(i18n.t('circles.peerMinutesAgo', { n: 3 }));
    });

    it('gives the button a name that contains what is visible, also while checking', async () => {
      // Label in name: somebody using voice control says what they SEE. An
      // aria-label of other words ("Check the reachability of … now") did not
      // contain "Check now", and stayed the same while the text said "Checking…".
      serve([reachable]);
      let answerCheck: () => void = () => {};
      mockedPost.mockImplementation(() => new Promise((resolve) => {
        answerCheck = () => resolve(response(reachable.reachability));
      }));
      renderWithRouter(<CirclesPeersPage />, { route: '/settings/circles/peers' });
      await screen.findByTestId('peer-reach-1');

      const button = checkNow();
      expect(button).not.toHaveAttribute('aria-label');
      expect(button).toHaveAccessibleName(`${i18n.t('circles.peerReachCheckNow')} Far Instance`);

      fireEvent.click(button);
      await waitFor(() => expect(button).toBeDisabled());
      expect(button).toHaveTextContent(i18n.t('circles.peerReachChecking'));
      expect(button).toHaveAccessibleName(`${i18n.t('circles.peerReachChecking')} Far Instance`);

      await act(async () => { answerCheck(); });
      await waitFor(() => expect(checkNow()).not.toBeDisabled());
    });

    it('keeps a fresh reading when a list fetch that was already under way lands after it', async () => {
      // The list answer was computed BEFORE the check ran. Landing after the
      // check's answer it used to put the old reading back -- and with an
      // unchecked row the page, having asked once, would never ask again.
      const queryClient = createTestQueryClient();
      let listCalls = 0;
      let releaseStaleList: () => void = () => {};
      mockedGet.mockImplementation((url: string) => {
        if (url === '/api/federation/links/mine') return Promise.resolve(response([]));
        if (url !== '/api/federation/peers') return Promise.reject(new Error(`unexpected GET ${url}`));
        listCalls += 1;
        if (listCalls === 1) return Promise.resolve(response({ peers: [reachable] }));
        return new Promise((resolve) => {
          releaseStaleList = () => resolve(response({ peers: [reachable] }));
        });
      });
      let answerCheck: () => void = () => {};
      mockedPost.mockImplementation(() => new Promise((resolve) => {
        answerCheck = () => resolve(
          response({ state: 'unreachable', reason: 'pin_mismatch', checked_at: minutesAgo(0) }),
        );
      }));
      renderWithRouter(<CirclesPeersPage />, { route: '/settings/circles/peers', queryClient });

      await screen.findByTestId('peer-reach-1');
      fireEvent.click(checkNow());
      await waitFor(() => expect(mockedPost).toHaveBeenCalledTimes(1));
      void queryClient.refetchQueries({ queryKey: keys.federation.peers() }).catch(() => {});
      await waitFor(() => expect(listCalls).toBe(2));

      await act(async () => { answerCheck(); });
      await waitFor(() => {
        expect(screen.getByTestId('peer-reach-1')).toHaveAttribute('data-reach-state', 'unreachable');
      });
      await act(async () => {
        releaseStaleList();
        await new Promise((resolve) => setTimeout(resolve, 20));
      });
      expect(screen.getByTestId('peer-reach-1')).toHaveAttribute('data-reach-state', 'unreachable');
    });

    it('starts no check at all when the backend sends no reachability', async () => {
      serve([legacyPeer]);
      renderWithRouter(<CirclesPeersPage />, { route: '/settings/circles/peers' });

      await screen.findByText('Far Instance');
      expect(mockedPost).not.toHaveBeenCalled();
    });

    it('reads a state or a reason this build does not know as unknown, without raw codes', async () => {
      serve([{
        ...reachable,
        reachability: { state: 'some_future_state', reason: 'some_future_code', checked_at: minutesAgo(3) },
      }]);
      renderWithRouter(<CirclesPeersPage />, { route: '/settings/circles/peers' });

      const line = await screen.findByTestId('peer-reach-1');
      expect(line).toHaveAttribute('data-reach-state', 'unknown');
      expect(line).not.toHaveTextContent('some_future');
    });

    it('marks a recently active peer and says when this side last asked', async () => {
      serve([{ ...reachable, recently_active: true, last_asked_at: minutesAgo(2) }]);
      renderWithRouter(<CirclesPeersPage />, { route: '/settings/circles/peers' });

      const chip = await screen.findByTestId('peer-recent-1');
      expect(chip).toHaveTextContent(i18n.t('circles.peerRecentlyActive'));
      expect(chip).toHaveAttribute('title', i18n.t('circles.peerRecentlyActiveHelp'));
      expect(
        screen.getByText(i18n.t('circles.peerLastAsked', {
          when: i18n.t('circles.peerMinutesAgo', { n: 2 }),
        })),
      ).toBeInTheDocument();
    });

    it('checks on demand and shows the answer without reloading the list', async () => {
      serve([reachable]);
      mockedPost.mockResolvedValue(
        response({ state: 'unreachable', reason: 'pin_mismatch', checked_at: minutesAgo(0) }),
      );
      renderWithRouter(<CirclesPeersPage />, { route: '/settings/circles/peers' });

      await screen.findByTestId('peer-reach-1');
      const listCalls = () =>
        mockedGet.mock.calls.filter(([url]) => url === '/api/federation/peers').length;
      const before = listCalls();
      fireEvent.click(checkNow());

      await waitFor(() => {
        expect(screen.getByTestId('peer-reach-1')).toHaveAttribute('data-reach-state', 'unreachable');
      });
      expect(mockedPost).toHaveBeenCalledWith('/api/federation/peers/1/check');
      expect(mockedPost).toHaveBeenCalledTimes(1);
      expect(screen.getByTestId('peer-reach-1')).toHaveTextContent(
        i18n.t('circles.peerReachReason.pin_mismatch'),
      );
      expect(listCalls()).toBe(before);
    });

    it('reports a check that could not be started and leaves the state as it was', async () => {
      serve([reachable]);
      mockedPost.mockRejectedValue(new Error('boom'));
      renderWithRouter(<CirclesPeersPage />, { route: '/settings/circles/peers' });

      await screen.findByTestId('peer-reach-1');
      fireEvent.click(checkNow());

      expect(await screen.findByText(i18n.t('circles.peerReachCheckFailed'))).toBeInTheDocument();
      expect(screen.getByTestId('peer-reach-1')).toHaveAttribute('data-reach-state', 'reachable');
      // The button is usable again.
      expect(checkNow()).not.toBeDisabled();
    });
  });

  it('has every reachability and instance-name key in all four locales', () => {
    const keys = [
      'peerReachState.reachable', 'peerReachState.unreachable', 'peerReachState.unknown',
      'peerReachReason.unreachable', 'peerReachReason.pin_mismatch', 'peerReachReason.refused',
      'peerReachReason.unsupported', 'peerReachReason.invalid_response',
      'peerReachReason.no_endpoint', 'peerReachReason.not_checked',
      'peerReachCheckedAt', 'peerReachCheckNow', 'peerReachChecking', 'peerReachCheckFailed',
      'peerRecentlyActive', 'peerRecentlyActiveHelp', 'peerLastAsked', 'peerLastAskedHelp',
    ];
    const elsewhere = [
      'kiosk.peer.state.reachable', 'kiosk.peer.state.unreachable', 'kiosk.peer.state.unknown',
      'kiosk.peer.recentlyActive',
      ...['waking_up', 'retrieving', 'synthesizing', 'complete', 'failed', 'fallback'].map(
        (label) => `chat.federationProgressInstance.${label}`,
      ),
    ];
    for (const lng of ['de', 'en', 'fr', 'it']) {
      for (const key of [...keys.map((k) => `circles.${k}`), ...elsewhere]) {
        expect(i18n.exists(key, { lng, fallbackLng: [] }), `${lng}: ${key}`).toBe(true);
      }
    }
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
