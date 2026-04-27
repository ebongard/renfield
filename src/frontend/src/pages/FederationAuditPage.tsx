import { useState, useEffect, useCallback, useMemo } from 'react';
import type { TFunction } from 'i18next';
import { useTranslation } from 'react-i18next';
import { Link, useSearchParams } from 'react-router';
import {
  History,
  Fingerprint,
  CheckCircle2,
  XCircle,
  HelpCircle,
  Clock,
  ChevronDown,
  ChevronRight,
  ShieldCheck,
  ShieldAlert,
} from 'lucide-react';
import apiClient from '../utils/axios';
import PageHeader from '../components/PageHeader';
import Alert from '../components/Alert';

type AuditStatus = 'success' | 'failed' | 'in_progress' | string;

interface AuditEntry {
  id: string;
  peer_pubkey: string;
  peer_display_name: string;
  query_text: string;
  answer_excerpt?: string;
  error_message?: string;
  initiated_at: string;
  finalized_at?: string;
  final_status: AuditStatus;
  verified_signature?: boolean;
}

interface StatusIconProps {
  status: AuditStatus;
  verified?: boolean;
}

/**
 * /brain/audit
 *
 * Asker-side federation audit feed: every federated query this user
 * has made, newest first. One row per query lifecycle (initiate →
 * terminal). Rows are read-only. Clicking expands to show the full
 * query and the answer excerpt / error text.
 *
 * Filter `?peer=<pubkey>` is honored so the peers page can deep-link
 * to "show me everything I asked this peer". Drop the filter with a
 * "show all" button next to the header.
 *
 * Retention is 90 days server-side (lifecycle cleanup); no client-side
 * pagination yet — the first 50 rows cover normal usage. If a
 * household user hits that limit we'll add a "Load more" button.
 */
const PAGE_SIZE = 50;

function StatusIcon({ status, verified }: StatusIconProps) {
  if (status === 'success') {
    return verified
      ? <CheckCircle2 className="w-4 h-4 text-green-500 dark:text-green-400" aria-hidden="true" />
      : <ShieldAlert className="w-4 h-4 text-amber-500 dark:text-amber-400" aria-hidden="true" />;
  }
  if (status === 'failed') {
    return <XCircle className="w-4 h-4 text-red-500 dark:text-red-400" aria-hidden="true" />;
  }
  return <HelpCircle className="w-4 h-4 text-gray-400 dark:text-gray-500" aria-hidden="true" />;
}

function formatDateTime(iso: string | undefined, lang: string): string {
  if (!iso) return '';
  try {
    const d = new Date(iso);
    return d.toLocaleString(lang === 'de' ? 'de-DE' : 'en-US', {
      dateStyle: 'short',
      timeStyle: 'short',
    });
  } catch {
    return iso;
  }
}

function formatRelativeDuration(
  startIso: string | undefined,
  endIso: string | undefined,
  t: TFunction,
): string | null {
  if (!startIso || !endIso) return null;
  try {
    const start = new Date(startIso).getTime();
    const end = new Date(endIso).getTime();
    const deltaMs = end - start;
    if (deltaMs < 0) return null;
    if (deltaMs < 1000) return t('federationAudit.durationSubSecond');
    if (deltaMs < 60000) return t('federationAudit.durationSeconds', { n: Math.round(deltaMs / 1000) });
    return t('federationAudit.durationMinutes', { n: Math.round(deltaMs / 60000) });
  } catch {
    return null;
  }
}

export default function FederationAuditPage() {
  const { t, i18n } = useTranslation();
  const [searchParams, setSearchParams] = useSearchParams();

  const peerFilter = searchParams.get('peer');

  const [entries, setEntries] = useState<AuditEntry[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [expanded, setExpanded] = useState<Set<string>>(() => new Set());

  const load = useCallback(async () => {
    try {
      setLoading(true);
      const params = new URLSearchParams({ limit: String(PAGE_SIZE) });
      if (peerFilter) params.set('peer_pubkey', peerFilter);
      const response = await apiClient.get<{ entries: AuditEntry[] }>(`/api/federation/audit?${params}`);
      setEntries(response.data.entries || []);
      setError(null);
    } catch {
      setError(t('federationAudit.loadFailed'));
    } finally {
      setLoading(false);
    }
  }, [peerFilter, t]);

  useEffect(() => {
    load();
  }, [load]);

  const toggleExpand = (id: string) => {
    setExpanded((prev) => {
      const next = new Set(prev);
      if (next.has(id)) next.delete(id); else next.add(id);
      return next;
    });
  };

  const clearPeerFilter = () => {
    const next = new URLSearchParams(searchParams);
    next.delete('peer');
    setSearchParams(next);
  };

  const filteredPeerName = useMemo(() => {
    if (!peerFilter || entries.length === 0) return null;
    const match = entries.find((e) => e.peer_pubkey === peerFilter);
    return match?.peer_display_name || peerFilter.slice(0, 12) + '…';
  }, [peerFilter, entries]);

  return (
    <div className="max-w-5xl mx-auto p-6 space-y-6">
      <PageHeader
        icon={History}
        title={t('federationAudit.title')}
        subtitle={t('federationAudit.subtitle')}
      />

      {error && <Alert variant="error" onClose={() => setError(null)}>{error}</Alert>}

      <div className="flex flex-wrap items-center justify-between gap-3 text-sm text-gray-500 dark:text-gray-400">
        <div>
          {peerFilter ? (
            <span>
              {t('federationAudit.filteredByPeer', { name: filteredPeerName })}{' '}
              <button
                type="button"
                onClick={clearPeerFilter}
                className="text-primary-600 hover:underline"
              >
                {t('federationAudit.clearFilter')}
              </button>
            </span>
          ) : (
            <span>{t('federationAudit.retentionNote')}</span>
          )}
        </div>
        <Link to="/settings/circles/peers" className="text-primary-600 hover:underline">
          {t('federationAudit.managePeersLink')}
        </Link>
      </div>

      {loading ? (
        <div className="text-center py-12 text-gray-500 dark:text-gray-400">
          {t('common.loading')}
        </div>
      ) : entries.length === 0 ? (
        <div className="card text-center py-12">
          <History className="w-12 h-12 mx-auto mb-3 text-gray-300 dark:text-gray-600" aria-hidden="true" />
          <p className="text-gray-500 dark:text-gray-400 mb-2">
            {t('federationAudit.emptyHeadline')}
          </p>
          <p className="text-sm text-gray-400 dark:text-gray-500">
            {t('federationAudit.emptyHint')}
          </p>
        </div>
      ) : (
        <ul className="space-y-2">
          {entries.map((entry) => {
            const isOpen = expanded.has(entry.id);
            const duration = formatRelativeDuration(entry.initiated_at, entry.finalized_at, t);
            return (
              <li key={entry.id} className="card p-0 overflow-hidden">
                <button
                  type="button"
                  onClick={() => toggleExpand(entry.id)}
                  className="w-full flex items-start gap-3 text-left p-3 hover:bg-gray-50 dark:hover:bg-gray-800/50"
                  aria-expanded={isOpen}
                >
                  {isOpen
                    ? <ChevronDown className="w-4 h-4 mt-1 flex-shrink-0 text-gray-400" aria-hidden="true" />
                    : <ChevronRight className="w-4 h-4 mt-1 flex-shrink-0 text-gray-400" aria-hidden="true" />
                  }
                  <div className="flex-1 min-w-0 space-y-1">
                    <div className="flex flex-wrap items-center gap-2">
                      <StatusIcon status={entry.final_status} verified={entry.verified_signature} />
                      <span className="font-medium text-gray-900 dark:text-white truncate">
                        {entry.peer_display_name}
                      </span>
                      <span className="text-xs text-gray-500 dark:text-gray-400 inline-flex items-center gap-1">
                        <Clock className="w-3 h-3" aria-hidden="true" />
                        {formatDateTime(entry.initiated_at, i18n.language)}
                      </span>
                      {duration && (
                        <span className="text-xs text-gray-400 dark:text-gray-500">
                          · {duration}
                        </span>
                      )}
                    </div>
                    <div className="text-sm text-gray-600 dark:text-gray-300 truncate">
                      {entry.query_text}
                    </div>
                  </div>
                </button>
                {isOpen && (
                  <div className="border-t border-gray-200 dark:border-gray-700 p-3 space-y-3 bg-gray-50 dark:bg-gray-800/30">
                    <div>
                      <div className="text-xs font-medium text-gray-500 dark:text-gray-400 mb-1">
                        {t('federationAudit.queryLabel')}
                      </div>
                      <p className="text-sm text-gray-900 dark:text-white whitespace-pre-wrap">
                        {entry.query_text}
                      </p>
                    </div>
                    {entry.answer_excerpt && (
                      <div>
                        <div className="text-xs font-medium text-gray-500 dark:text-gray-400 mb-1 inline-flex items-center gap-1">
                          {entry.verified_signature && (
                            <ShieldCheck className="w-3 h-3 text-green-500 dark:text-green-400" aria-hidden="true" />
                          )}
                          {t('federationAudit.answerLabel')}
                        </div>
                        <p className="text-sm text-gray-900 dark:text-white whitespace-pre-wrap">
                          {entry.answer_excerpt}
                        </p>
                      </div>
                    )}
                    {entry.error_message && (
                      <div>
                        <div className="text-xs font-medium text-red-500 dark:text-red-400 mb-1">
                          {t('federationAudit.errorLabel')}
                        </div>
                        <p className="text-sm text-red-600 dark:text-red-400 whitespace-pre-wrap">
                          {entry.error_message}
                        </p>
                      </div>
                    )}
                    <div className="text-xs text-gray-500 dark:text-gray-400 inline-flex items-center gap-2 flex-wrap">
                      <span className="inline-flex items-center gap-1">
                        <Fingerprint className="w-3 h-3" aria-hidden="true" />
                        <code className="tabular-nums">{entry.peer_pubkey.slice(0, 12)}…</code>
                      </span>
                      <span>
                        {t(`federationAudit.status.${entry.final_status}`, { defaultValue: entry.final_status })}
                      </span>
                    </div>
                  </div>
                )}
              </li>
            );
          })}
        </ul>
      )}
    </div>
  );
}
