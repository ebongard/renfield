import { useState, useEffect, useRef, useCallback } from 'react';
import { useTranslation } from 'react-i18next';
import { Link } from 'react-router';
import {
  Users, Trash2, Clock, Fingerprint, History, Link2, Link2Off, UserRound,
  CheckCircle, XCircle, HelpCircle, RefreshCw, Activity, Send,
} from 'lucide-react';
import PageHeader from '../components/PageHeader';
import Alert from '../components/Alert';
import TierBadge from '../components/TierBadge';
import { useConfirmDialog } from '../components/ConfirmDialog';
import LinkIdentityInitiatorModal from '../components/LinkIdentityInitiatorModal';
import LinkIdentityResponderModal from '../components/LinkIdentityResponderModal';
import {
  useFederationPeersQuery,
  useDeleteFederationPeer,
  useMyIdentityLinksQuery,
  useDeleteIdentityLink,
  useCheckFederationPeer,
  type FederationPeer,
  type IdentityLink,
} from '../api/resources/federation';
import { extractApiError } from '../utils/axios';
import { formatDate, toLocalDate } from '../utils/datetime';
import { formatPeerLabel, peerLabelParts } from '../utils/peerLabel';

/** How each reachability state looks: a symbol AND a colour (never colour
 *  alone), in the semantic colours of DESIGN.md -- success = turquoise,
 *  error = crimson, and "unknown" deliberately neutral: it is not a fault. */
const REACH_LOOK = {
  reachable: { Icon: CheckCircle, className: 'text-accent-700 dark:text-accent-300' },
  unreachable: { Icon: XCircle, className: 'text-primary-700 dark:text-primary-300' },
  unknown: { Icon: HelpCircle, className: 'text-gray-500 dark:text-gray-400' },
} as const;

type ReachState = keyof typeof REACH_LOOK;

/** A state this build does not know is UNKNOWN -- never "unreachable". */
function reachStateOf(state: string): ReachState {
  return state === 'reachable' || state === 'unreachable' ? state : 'unknown';
}

export default function CirclesPeersPage() {
  const { t } = useTranslation();
  const { confirm, ConfirmDialogComponent } = useConfirmDialog();

  const peersQuery = useFederationPeersQuery();
  const peers = peersQuery.data ?? [];

  const deletePeer = useDeleteFederationPeer();
  const { mutateAsync: checkPeerAsync } = useCheckFederationPeer();

  // F-ID-2: the person's own links. Deliberately HERE and not on a page of
  // their own -- a link always hangs off a pairing, and whoever wants to
  // revoke one looks for it where the far side is listed.
  const linksQuery = useMyIdentityLinksQuery();
  const links = linksQuery.data ?? [];
  const deleteLink = useDeleteIdentityLink();
  const [linkPeer, setLinkPeer] = useState<FederationPeer | null>(null);
  const [showAcceptLink, setShowAcceptLink] = useState(false);

  const [error, setError] = useState<string | null>(null);
  const [success, setSuccess] = useState<string | null>(null);
  const [revokingIds, setRevokingIds] = useState<Set<string>>(() => new Set());
  const [checkingIds, setCheckingIds] = useState<Set<string>>(() => new Set());

  useEffect(() => {
    if (success) {
      const timer = setTimeout(() => setSuccess(null), 3000);
      return () => clearTimeout(timer);
    }
  }, [success]);

  const displayError = error ?? peersQuery.errorMessage;

  // `last_seen_at` is written when the peer sends US an authenticated
  // request. It says nothing about reachability, so the label names the
  // event ("last request ...") instead of reading like a connection state.
  const formatLastRequest = (iso?: string | null): string => {
    if (!iso) return t('circles.peerNeverAsked');
    return t('circles.peerLastRequest', { when: relativeWhen(iso) });
  };

  // "just now" / "5m ago" / ... for a server timestamp; the raw value when it
  // cannot be parsed, so nothing is silently dropped.
  const relativeWhen = (iso: string): string => {
    const when = toLocalDate(iso);
    if (!when) return iso;
    const minutes = Math.floor((Date.now() - when.getTime()) / 60000);
    if (minutes < 1) return t('circles.peerJustNow');
    if (minutes < 60) return t('circles.peerMinutesAgo', { n: minutes });
    const hours = Math.floor(minutes / 60);
    if (hours < 24) return t('circles.peerHoursAgo', { n: hours });
    return t('circles.peerDaysAgo', { n: Math.floor(hours / 24) });
  };

  // One signed round trip on demand. The answer lands in the cached list (see
  // `useCheckFederationPeer`), so the row redraws by itself. `quiet` = nobody
  // pressed anything: a failure leaves the row as it was and raises no alert.
  const runCheck = useCallback(async (peerId: string, quiet: boolean) => {
    setCheckingIds((prev) => new Set(prev).add(peerId));
    try {
      await checkPeerAsync(peerId);
    } catch (err) {
      if (!quiet) setError(extractApiError(err, t('circles.peerReachCheckFailed')));
    } finally {
      setCheckingIds((prev) => {
        const next = new Set(prev);
        next.delete(peerId);
        return next;
      });
    }
  }, [checkPeerAsync, t]);

  // The readings are kept current only while a wall display is connected (no
  // free-running poll on the server). A row without a reading -- never
  // checked, or the last one expired -- is therefore checked ONCE when this
  // page shows it: a person opening the page is the demand. Once per row and
  // page visit; the server caps it per pairing on top.
  const autoChecked = useRef<Set<string>>(new Set());
  const peerList = peersQuery.data;
  useEffect(() => {
    for (const peer of peerList ?? []) {
      if (!peer.reachability || peer.reachability.checked_at) continue;
      if (autoChecked.current.has(peer.id)) continue;
      autoChecked.current.add(peer.id);
      void runCheck(peer.id, true);
    }
  }, [peerList, runCheck]);

  const handleRevokeLink = async (link: IdentityLink) => {
    const ok = await confirm({
      title: t('circles.revokeLinkTitle'),
      message: t('circles.revokeLinkConfirm', {
        name: formatPeerLabel(link.peer_instance_name, link.peer_display_name),
      }),
      confirmLabel: t('common.delete'),
      variant: 'danger',
    });
    if (!ok) return;
    try {
      await deleteLink.mutateAsync(link.id);
      setSuccess(t('circles.linkRevoked'));
    } catch (err) {
      setError(extractApiError(err, t('circles.linkRevokeFailed')));
    }
  };

  const handleRevoke = async (peer: FederationPeer) => {
    // Someone else's pairing (an admin revoking): the standard text speaks of
    // "your shared information", which would be wrong here.
    const foreign = peer.owned_by_me === false;
    // Whoever revokes must see WHICH pairing: the full label, instance and person.
    const name = formatPeerLabel(peer.instance_name, peer.remote_display_name);
    const ok = await confirm({
      title: t('circles.revokePeerTitle'),
      message: foreign
        ? t('circles.revokePeerConfirmOther', { name, pairedBy: peer.paired_by ?? '' })
        : t('circles.revokePeerConfirm', { name }),
      confirmLabel: t('common.delete'),
      variant: 'danger',
    });
    if (!ok) return;

    setRevokingIds((prev) => new Set(prev).add(peer.id));
    try {
      await deletePeer.mutateAsync(peer.id);
      setSuccess(t('circles.peerRevoked', { name }));
    } catch (err) {
      setError(extractApiError(err, t('circles.peerRevokeFailed')));
    } finally {
      setRevokingIds((prev) => {
        const next = new Set(prev);
        next.delete(peer.id);
        return next;
      });
    }
  };

  return (
    <div className="max-w-5xl mx-auto p-6 space-y-6">
      <PageHeader
        icon={Users}
        title={t('circles.peersTitle')}
        subtitle={t('circles.peersSubtitle')}
      />

      {displayError && <Alert variant="error" onClose={() => setError(null)}>{displayError}</Alert>}
      {success && <Alert variant="success">{success}</Alert>}

      <div className="text-sm text-gray-500 dark:text-gray-400">
        {t('circles.peersManageHint')}{' '}
        <Link to="/settings/circles" className="text-primary-600 hover:underline">
          /settings/circles
        </Link>
        .
      </div>

      {peersQuery.isLoading ? (
        <div className="text-center py-12 text-gray-500 dark:text-gray-400">
          {t('common.loading')}
        </div>
      ) : peers.length === 0 ? (
        <div className="card text-center py-12">
          <Users className="w-12 h-12 mx-auto mb-3 text-gray-300 dark:text-gray-600" aria-hidden="true" />
          <p className="text-gray-500 dark:text-gray-400 mb-2">
            {t('circles.peersEmptyHeadline')}
          </p>
          <p className="text-sm text-gray-400 dark:text-gray-500">
            {t('circles.peersEmptyHint')}
          </p>
        </div>
      ) : (
        <ul className="space-y-3 animate-stagger">
          {peers.map((peer) => {
            // The instance lists every account's pairings only when the
            // response carries the additive fields. Without them the row is
            // the caller's own pairing and renders exactly as before.
            const showsAllPairings = typeof peer.owned_by_me === 'boolean';
            const ownedByMe = peer.owned_by_me ?? true;
            const canRevoke = peer.can_revoke ?? true;
            const tierLabel = t(`circles.tier.${Math.max(0, Math.min(4, Number(peer.circle_tier) || 0))}`);
            // Instance first, person second; the person alone when the far
            // instance has no label -- then the row reads as it always did.
            const label = peerLabelParts(peer.instance_name, peer.remote_display_name);
            // In running text the peer is called by its primary name; the
            // heading right above carries the full label.
            const name = label.primary;
            // Present only while the instance runs the signed check.
            const reach = peer.reachability ?? null;
            const reachState = reach ? reachStateOf(reach.state) : null;
            const reachLook = reachState ? REACH_LOOK[reachState] : null;
            // A code this build has no text for: the state stands alone.
            const reachReason = reach?.reason
              ? t(`circles.peerReachReason.${reach.reason}`, { defaultValue: '' })
              : '';
            const checking = checkingIds.has(peer.id);
            return (
              <li
                key={peer.id}
                className={`atom-row tier-ring-${peer.circle_tier} flex-col sm:flex-row`}
              >
                <div className="flex-1 min-w-0 space-y-2">
                  <div className="flex flex-wrap items-center gap-2">
                    <span className="font-medium text-gray-900 dark:text-white truncate">
                      {label.primary}
                    </span>
                    {label.secondary && (
                      <span
                        className="text-sm text-gray-500 dark:text-gray-400 truncate"
                        data-testid={`peer-person-${peer.id}`}
                      >
                        · {label.secondary}
                      </span>
                    )}
                    <TierBadge tier={peer.circle_tier} />
                  </div>
                  {reach && reachState && reachLook && (
                    <div
                      className="flex flex-wrap items-center gap-x-3 gap-y-1 text-xs"
                      data-testid={`peer-reach-${peer.id}`}
                      data-reach-state={reachState}
                    >
                      <span className={`inline-flex items-center gap-1 font-medium ${reachLook.className}`}>
                        <reachLook.Icon className="w-3.5 h-3.5" aria-hidden="true" />
                        {t(`circles.peerReachState.${reachState}`)}
                      </span>
                      {reachReason && (
                        <span className="text-gray-600 dark:text-gray-300">{reachReason}</span>
                      )}
                      {reach.checked_at && (
                        <span
                          className="text-gray-500 dark:text-gray-400"
                          data-testid={`peer-reach-checked-${peer.id}`}
                        >
                          {t('circles.peerReachCheckedAt', { when: relativeWhen(reach.checked_at) })}
                        </span>
                      )}
                      <button
                        type="button"
                        onClick={() => runCheck(peer.id, false)}
                        disabled={checking}
                        className="inline-flex items-center gap-1 text-primary-600 dark:text-primary-400 hover:underline disabled:opacity-50 disabled:no-underline"
                      >
                        <RefreshCw
                          className={`w-3 h-3 ${checking ? 'motion-safe:animate-spin' : ''}`}
                          aria-hidden="true"
                        />
                        {checking ? t('circles.peerReachChecking') : t('circles.peerReachCheckNow')}
                      </button>
                    </div>
                  )}
                  <div className="flex flex-wrap items-center gap-3 text-xs text-gray-500 dark:text-gray-400">
                    <span className="inline-flex items-center gap-1">
                      <Fingerprint className="w-3 h-3" aria-hidden="true" />
                      <code className="tabular-nums">{peer.remote_pubkey.slice(0, 12)}…</code>
                    </span>
                    <span
                      className="inline-flex items-center gap-1"
                      title={t('circles.peerLastRequestHelp')}
                    >
                      <Clock className="w-3 h-3" aria-hidden="true" />
                      {formatLastRequest(peer.last_seen_at)}
                    </span>
                    {peer.last_asked_at && (
                      <span
                        className="inline-flex items-center gap-1"
                        title={t('circles.peerLastAskedHelp')}
                      >
                        <Send className="w-3 h-3" aria-hidden="true" />
                        {t('circles.peerLastAsked', { when: relativeWhen(peer.last_asked_at) })}
                      </span>
                    )}
                    {peer.recently_active && (
                      <span
                        className="inline-flex items-center gap-1 text-accent-700 dark:text-accent-300"
                        title={t('circles.peerRecentlyActiveHelp')}
                        data-testid={`peer-recent-${peer.id}`}
                      >
                        <Activity className="w-3 h-3" aria-hidden="true" />
                        {t('circles.peerRecentlyActive')}
                      </span>
                    )}
                    {showsAllPairings && !ownedByMe && (
                      <span className="inline-flex items-center gap-1">
                        <UserRound className="w-3 h-3" aria-hidden="true" />
                        {t('circles.peerPairedBy', { name: peer.paired_by ?? '' })}
                      </span>
                    )}
                  </div>
                  {showsAllPairings && (
                    <div className="space-y-1 text-xs text-gray-600 dark:text-gray-300">
                      <p data-testid={`peer-ask-hint-${peer.id}`}>
                        {peer.linked
                          ? t('circles.peerAskLinked', { name })
                          : t('circles.peerAskUnlinked', { name })}
                      </p>
                      <p data-testid={`peer-grant-hint-${peer.id}`}>
                        {ownedByMe
                          ? t('circles.peerGrantMine', { name, tier: tierLabel })
                          : t('circles.peerGrantOther', {
                              name,
                              pairedBy: peer.paired_by ?? '',
                              tier: tierLabel,
                            })}
                      </p>
                    </div>
                  )}
                </div>
                <div className="sm:ml-4 sm:flex-shrink-0 flex items-center gap-2 mt-3 sm:mt-0">
                  <Link
                    to={`/brain/audit?peer=${encodeURIComponent(peer.remote_pubkey)}`}
                    className="btn-icon btn-icon-ghost"
                    title={t('circles.peerShowAudit')}
                    aria-label={t('circles.peerShowAudit')}
                  >
                    <History className="w-4 h-4" />
                  </Link>
                  {/* The person handshake presupposes a pairing the person
                      OWNS (the server answers 404 otherwise), so the button
                      is offered on own pairings only. */}
                  {ownedByMe && (
                    <button
                      type="button"
                      onClick={() => setLinkPeer(peer)}
                      className="btn-icon btn-icon-ghost"
                      title={t('circles.linkStartHandshake')}
                      aria-label={t('circles.linkStartHandshake')}
                    >
                      <Link2 className="w-4 h-4" />
                    </button>
                  )}
                  {canRevoke && (
                    <button
                      type="button"
                      onClick={() => handleRevoke(peer)}
                      disabled={revokingIds.has(peer.id)}
                      className="btn-icon btn-icon-danger disabled:opacity-50"
                      title={t('circles.revokePeer')}
                      aria-label={t('circles.revokePeer')}
                    >
                      <Trash2 className="w-4 h-4" />
                    </button>
                  )}
                </div>
              </li>
            );
          })}
        </ul>
      )}

      {/* ── Verknuepfte Identitaeten (F-ID-2) ─────────────────────────────── */}
      <section className="space-y-3 pt-2">
        <div className="flex flex-wrap items-center justify-between gap-2">
          <div>
            <h2 className="text-lg font-semibold text-gray-900 dark:text-white">
              {t('circles.linksTitle')}
            </h2>
            <p className="text-sm text-gray-500 dark:text-gray-400">
              {t('circles.linksSubtitle')}
            </p>
          </div>
          <button
            type="button"
            onClick={() => setShowAcceptLink(true)}
            className="btn-secondary px-3 py-2 rounded-lg text-sm inline-flex items-center gap-2"
          >
            <Link2 className="w-4 h-4" aria-hidden="true" />
            {t('circles.linkAcceptHandshake')}
          </button>
        </div>

        {links.length === 0 ? (
          <div className="card text-center py-8">
            <Link2Off className="w-10 h-10 mx-auto mb-3 text-gray-300 dark:text-gray-600" aria-hidden="true" />
            <p className="text-gray-500 dark:text-gray-400 mb-2">
              {t('circles.linksEmptyHeadline')}
            </p>
            <p className="text-sm text-gray-400 dark:text-gray-500">
              {t('circles.linksEmptyHint')}
            </p>
          </div>
        ) : (
          <ul className="space-y-3">
            {links.map((link) => (
              <li key={link.id} className="atom-row flex-col sm:flex-row">
                <div className="flex-1 min-w-0">
                  <span className="font-medium text-gray-900 dark:text-white truncate">
                    {formatPeerLabel(link.peer_instance_name, link.peer_display_name)}
                  </span>
                  <div className="text-xs text-gray-500 dark:text-gray-400">
                    {t('circles.linkCreatedAt', {
                      date: formatDate(link.created_at) || '—',
                    })}
                  </div>
                </div>
                <button
                  type="button"
                  onClick={() => handleRevokeLink(link)}
                  className="btn-icon btn-icon-danger sm:ml-4 sm:flex-shrink-0 mt-3 sm:mt-0"
                  title={t('circles.revokeLink')}
                  aria-label={t('circles.revokeLink')}
                >
                  <Trash2 className="w-4 h-4" />
                </button>
              </li>
            ))}
          </ul>
        )}
      </section>

      <LinkIdentityInitiatorModal
        isOpen={linkPeer !== null}
        peerId={linkPeer ? Number(linkPeer.id) : null}
        /* Running text ("your account at <peer>"): the primary name. */
        peerName={linkPeer ? peerLabelParts(linkPeer.instance_name, linkPeer.remote_display_name).primary : undefined}
        onClose={() => setLinkPeer(null)}
        onLinked={() => { setSuccess(t('circles.linkSuccess')); linksQuery.refetch(); }}
      />
      <LinkIdentityResponderModal
        isOpen={showAcceptLink}
        onClose={() => setShowAcceptLink(false)}
        /* 🛑 NOT `linkSuccess`. On accepting, only OUR half exists; the link
           takes effect with the completion on the other side. The first
           version reported "the identities are linked" here -- contradicting
           the hint in the same dialog, which says it correctly. Anyone reading
           that closes both windows and leaves half a handshake behind. */
        onLinked={() => { setSuccess(t('circles.linkHalfDone')); linksQuery.refetch(); }}
      />

      {ConfirmDialogComponent}
    </div>
  );
}
