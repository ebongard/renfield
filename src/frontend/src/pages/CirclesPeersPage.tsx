import { useState, useEffect, useCallback } from 'react';
import { useTranslation } from 'react-i18next';
import { Link } from 'react-router';
import { Users, Trash2, Clock, Fingerprint, History } from 'lucide-react';
import apiClient from '../utils/axios';
import PageHeader from '../components/PageHeader';
import Alert from '../components/Alert';
import TierBadge from '../components/TierBadge';
import type { CircleTier } from '../components/TierBadge';
import { useConfirmDialog } from '../components/ConfirmDialog';

interface FederationPeer {
  id: string;
  remote_display_name: string;
  remote_pubkey: string;
  circle_tier: CircleTier | number;
  last_seen_at?: string | null;
}

/**
 * /settings/circles/peers
 *
 * Lists paired Renfield peers (from F2 pairing). Shows display_name +
 * pubkey fingerprint + tier-granted + last-seen. Revoke button
 * terminates the pairing (F4a — backend calls PeerUser.revoked_at + deletes
 * the CircleMembership + refreshes the MCP registry so the agent-loop
 * tool disappears).
 *
 * Pairing creation (QR handshake) lives on the sibling /settings/circles
 * page as a modal — this page is the post-pair management surface.
 */
export default function CirclesPeersPage() {
  const { t, i18n } = useTranslation();
  const { confirm, ConfirmDialogComponent } = useConfirmDialog();

  const [peers, setPeers] = useState<FederationPeer[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [success, setSuccess] = useState<string | null>(null);
  const [revokingIds, setRevokingIds] = useState<Set<string>>(() => new Set());

  const load = useCallback(async () => {
    try {
      setLoading(true);
      const response = await apiClient.get<{ peers: FederationPeer[] }>('/api/federation/peers');
      setPeers(response.data.peers || []);
      setError(null);
    } catch {
      setError(t('circles.peersCouldNotLoad'));
    } finally {
      setLoading(false);
    }
  }, [t]);

  useEffect(() => {
    load();
  }, [load]);

  useEffect(() => {
    if (success) {
      const timer = setTimeout(() => setSuccess(null), 3000);
      return () => clearTimeout(timer);
    }
  }, [success]);

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
      await apiClient.delete(`/api/federation/peers/${peer.id}`);
      setPeers((prev) => prev.filter((p) => p.id !== peer.id));
      setSuccess(t('circles.peerRevoked', { name: peer.remote_display_name }));
    } catch {
      setError(t('circles.peerRevokeFailed'));
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

      {error && <Alert variant="error" onClose={() => setError(null)}>{error}</Alert>}
      {success && <Alert variant="success">{success}</Alert>}

      <div className="text-sm text-gray-500 dark:text-gray-400">
        {t('circles.peersManageHint')}{' '}
        <Link to="/settings/circles" className="text-primary-600 hover:underline">
          /settings/circles
        </Link>
        .
      </div>

      {loading ? (
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

      {ConfirmDialogComponent}
    </div>
  );
}
