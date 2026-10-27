import { useState, useEffect } from 'react';
import { useTranslation } from 'react-i18next';
import { Link } from 'react-router';
import { Users, Trash2, Clock, Fingerprint, History, Link2, Link2Off } from 'lucide-react';
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
  type FederationPeer,
  type IdentityLink,
} from '../api/resources/federation';
import { extractApiError } from '../utils/axios';

export default function CirclesPeersPage() {
  const { t } = useTranslation();
  const { confirm, ConfirmDialogComponent } = useConfirmDialog();

  const peersQuery = useFederationPeersQuery();
  const peers = peersQuery.data ?? [];

  const deletePeer = useDeleteFederationPeer();

  // F-ID-2: die eigenen Personenverknuepfungen. Bewusst HIER und nicht auf einer
  // eigenen Seite — eine Verknuepfung haengt immer an einer Paarung, und wer sie
  // widerrufen will, sucht sie dort, wo die Gegenstelle steht.
  const linksQuery = useMyIdentityLinksQuery();
  const links = linksQuery.data ?? [];
  const deleteLink = useDeleteIdentityLink();
  const [linkPeer, setLinkPeer] = useState<FederationPeer | null>(null);
  const [showAcceptLink, setShowAcceptLink] = useState(false);

  const [error, setError] = useState<string | null>(null);
  const [success, setSuccess] = useState<string | null>(null);
  const [revokingIds, setRevokingIds] = useState<Set<string>>(() => new Set());

  useEffect(() => {
    if (success) {
      const timer = setTimeout(() => setSuccess(null), 3000);
      return () => clearTimeout(timer);
    }
  }, [success]);

  const displayError = error ?? peersQuery.errorMessage;

  const formatRelative = (iso?: string | null): string => {
    if (!iso) return t('circles.peerNeverSeen');
    try {
      const when = new Date(iso);
      const diff = Date.now() - when.getTime();
      const minutes = Math.floor(diff / 60000);
      if (minutes < 1) return t('circles.peerJustNow');
      if (minutes < 60) return t('circles.peerMinutesAgo', { n: minutes });
      const hours = Math.floor(minutes / 60);
      if (hours < 24) return t('circles.peerHoursAgo', { n: hours });
      const days = Math.floor(hours / 24);
      return t('circles.peerDaysAgo', { n: days });
    } catch {
      return iso;
    }
  };

  const handleRevokeLink = async (link: IdentityLink) => {
    const ok = await confirm({
      title: t('circles.revokeLinkTitle'),
      message: t('circles.revokeLinkConfirm', { name: link.peer_display_name }),
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
    const ok = await confirm({
      title: t('circles.revokePeerTitle'),
      message: t('circles.revokePeerConfirm', { name: peer.remote_display_name }),
      confirmLabel: t('common.delete'),
      variant: 'danger',
    });
    if (!ok) return;

    setRevokingIds((prev) => new Set(prev).add(peer.id));
    try {
      await deletePeer.mutateAsync(peer.id);
      setSuccess(t('circles.peerRevoked', { name: peer.remote_display_name }));
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
          {peers.map((peer) => (
            <li
              key={peer.id}
              className={`atom-row tier-ring-${peer.circle_tier} flex-col sm:flex-row`}
            >
              <div className="flex-1 min-w-0 space-y-2">
                <div className="flex flex-wrap items-center gap-2">
                  <span className="font-medium text-gray-900 dark:text-white truncate">
                    {peer.remote_display_name}
                  </span>
                  <TierBadge tier={peer.circle_tier} />
                </div>
                <div className="flex flex-wrap items-center gap-3 text-xs text-gray-500 dark:text-gray-400">
                  <span className="inline-flex items-center gap-1">
                    <Fingerprint className="w-3 h-3" aria-hidden="true" />
                    <code className="tabular-nums">{peer.remote_pubkey.slice(0, 12)}…</code>
                  </span>
                  <span className="inline-flex items-center gap-1">
                    <Clock className="w-3 h-3" aria-hidden="true" />
                    {formatRelative(peer.last_seen_at)}
                  </span>
                </div>
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
                <button
                  type="button"
                  onClick={() => setLinkPeer(peer)}
                  className="btn-icon btn-icon-ghost"
                  title={t('circles.linkStartHandshake')}
                  aria-label={t('circles.linkStartHandshake')}
                >
                  <Link2 className="w-4 h-4" />
                </button>
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
              </div>
            </li>
          ))}
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
                    {link.peer_display_name}
                  </span>
                  <div className="text-xs text-gray-500 dark:text-gray-400">
                    {t('circles.linkCreatedAt', {
                      date: link.created_at ? new Date(link.created_at).toLocaleDateString() : '—',
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
        peerName={linkPeer?.remote_display_name}
        onClose={() => setLinkPeer(null)}
        onLinked={() => { setSuccess(t('circles.linkSuccess')); linksQuery.refetch(); }}
      />
      <LinkIdentityResponderModal
        isOpen={showAcceptLink}
        onClose={() => setShowAcceptLink(false)}
        /* 🛑 NICHT `linkSuccess`. Beim Annehmen steht erst die EIGENE Haelfte;
           wirksam wird die Verknuepfung mit dem Abschluss drueben. Die erste
           Fassung meldete hier „Die Identitaeten sind verknuepft" — und
           widersprach damit dem Hinweis im selben Dialog, der es richtig sagt.
           Wer das liest, schliesst beide Fenster und laesst einen halben
           Handschlag zurueck. */
        onLinked={() => { setSuccess(t('circles.linkHalfDone')); linksQuery.refetch(); }}
      />

      {ConfirmDialogComponent}
    </div>
  );
}
