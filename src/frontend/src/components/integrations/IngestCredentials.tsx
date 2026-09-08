/**
 * Per-integration ingest credentials (#1218 Phase 2).
 *
 * Closes the gap that provisioning a machine credential meant hand-crafting an
 * API call. Mirrors SatelliteEnrollment's show-once UX, including its
 * failed-clipboard handling: the token is displayed exactly once, so a silently
 * failed copy would lose it.
 *
 * Two behaviours here are deliberate and load-bearing:
 *
 *  - Rotating shows WHICH client stops working until its copy is updated. The
 *    credential is shared with nothing else, but the client itself keeps
 *    presenting the old token until an operator updates it.
 *  - A legacy token whose source is `env` is rendered READ-ONLY. The backend
 *    re-seeds it from its environment on every boot, so a rotation here would
 *    silently revert at the next restart — the UI must not offer an action it
 *    cannot make stick.
 */
import { useState } from 'react';
import { useTranslation } from 'react-i18next';

import {
  useIngestCredentialsQuery,
  useMintIngestCredential,
  useRotateIngestCredential,
  useRevokeIngestCredential,
  type IngestCredential,
  type IngestRoute,
  type MintResult,
} from '../../api/resources/ingestCredentials';
import { formatDateTime } from '../../utils/datetime';
import Alert from '../Alert';
import Badge from '../Badge';

const ROUTES: IngestRoute[] = ['folder_ingest', 'email_ingest'];

export default function IngestCredentials() {
  const { t } = useTranslation();
  const { data, isLoading } = useIngestCredentialsQuery();
  const mint = useMintIngestCredential();
  const rotate = useRotateIngestCredential();
  const revoke = useRevokeIngestCredential();

  const [clientId, setClientId] = useState('');
  const [label, setLabel] = useState('');
  const [route, setRoute] = useState<IngestRoute>('folder_ingest');
  const [minted, setMinted] = useState<(MintResult & { rotated: boolean }) | null>(null);
  const [copied, setCopied] = useState(false);
  const [copyFailed, setCopyFailed] = useState(false);
  const [confirming, setConfirming] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);

  const credentials = data?.credentials ?? [];
  const legacy = data?.legacy ?? [];
  const flagOn = data?.enabled ?? false;

  const reveal = (result: MintResult, rotated: boolean) => {
    setMinted({ ...result, rotated });
    setCopied(false);
    setCopyFailed(false);
    setError(null);
  };

  const doMint = async () => {
    try {
      reveal(await mint.mutateAsync({ client_id: clientId.trim(), label: label.trim() || clientId.trim(), route }), false);
      setClientId('');
      setLabel('');
    } catch {
      setError(t('integrations.credentials.mintFailed'));
    }
  };

  const doRotate = async (id: string) => {
    setConfirming(null);
    try {
      reveal(await rotate.mutateAsync(id), true);
    } catch {
      setError(t('integrations.credentials.rotateFailed'));
    }
  };

  const copyToken = async () => {
    if (!minted) return;
    try {
      await navigator.clipboard.writeText(minted.token);
      setCopied(true);
      setCopyFailed(false);
    } catch {
      // Shown ONCE — a silently failed copy loses it. Tell the user to select
      // it manually rather than leaving the button apparently inert.
      setCopied(false);
      setCopyFailed(true);
    }
  };

  const fmt = (iso: string | null) => (iso ? formatDateTime(iso) : t('integrations.credentials.never'));

  const stateBadge = (c: IngestCredential) => {
    if (c.revoked_at || !c.is_enabled) return <Badge color="red">{t('integrations.credentials.revoked')}</Badge>;
    if (!c.last_authenticated_at) return <Badge color="yellow">{t('integrations.credentials.neverUsed')}</Badge>;
    return <Badge color="green">{t('integrations.credentials.active')}</Badge>;
  };

  return (
    <div className="card mt-6">
      <h3 className="text-lg font-semibold text-gray-900 dark:text-white">
        {t('integrations.credentials.title')}
      </h3>
      <p className="mt-1 text-sm text-gray-500 dark:text-gray-400">
        {t('integrations.credentials.description')}
      </p>

      {!flagOn && (
        <Alert variant="info" className="mt-3">
          {t('integrations.credentials.flagOff')}
        </Alert>
      )}
      {error && <Alert variant="error" className="mt-3">{error}</Alert>}

      {/* One-time token reveal */}
      {minted && (
        <div className="mt-4 rounded-lg border border-amber-300 bg-amber-50 p-3 dark:border-amber-700 dark:bg-amber-900/20">
          <p className="text-sm font-medium text-amber-800 dark:text-amber-300">
            {minted.rotated
              ? t('integrations.credentials.tokenRotated', { id: minted.client_id })
              : t('integrations.credentials.tokenMinted', { id: minted.client_id })}
          </p>
          <p className="mt-1 text-xs text-amber-700 dark:text-amber-400">
            {t('integrations.credentials.tokenOnceWarning')}
          </p>
          <div className="mt-2 flex items-center gap-2">
            <code className="flex-1 break-all rounded-sm bg-white px-2 py-1 font-mono text-xs text-gray-800 dark:bg-gray-800 dark:text-gray-100">
              {minted.token}
            </code>
            <button className="btn-secondary text-sm" onClick={copyToken}>
              {copied ? t('integrations.credentials.copied') : t('integrations.credentials.copy')}
            </button>
          </div>
          {copyFailed && (
            <p className="mt-2 text-xs text-red-600 dark:text-red-400">
              {t('integrations.credentials.copyFailed')}
            </p>
          )}
        </div>
      )}

      {/* Mint */}
      <div className="mt-4 flex flex-wrap items-end gap-2">
        <label className="flex flex-col text-xs text-gray-500 dark:text-gray-400">
          {t('integrations.credentials.clientId')}
          <input className="input mt-1 w-40" value={clientId} placeholder="scanner"
                 onChange={(e) => setClientId(e.target.value)} />
        </label>
        <label className="flex flex-col text-xs text-gray-500 dark:text-gray-400">
          {t('integrations.credentials.label')}
          <input className="input mt-1 w-48" value={label}
                 onChange={(e) => setLabel(e.target.value)} />
        </label>
        <label className="flex flex-col text-xs text-gray-500 dark:text-gray-400">
          {t('integrations.credentials.route')}
          <select className="input mt-1 w-44" value={route}
                  onChange={(e) => setRoute(e.target.value as IngestRoute)}>
            {ROUTES.map((r) => (
              <option key={r} value={r}>{t(`integrations.credentials.routes.${r}`)}</option>
            ))}
          </select>
        </label>
        <button className="btn-primary" disabled={!clientId.trim() || mint.isPending} onClick={doMint}>
          {t('integrations.credentials.mint')}
        </button>
      </div>

      {isLoading && (
        <p className="mt-4 text-sm text-gray-500 dark:text-gray-400">{t('common.loading')}</p>
      )}

      {credentials.length > 0 && (
        <ul className="mt-5 divide-y divide-gray-200 dark:divide-gray-700">
          {credentials.map((c) => (
            <li key={c.client_id} className="py-3">
              <div className="flex flex-wrap items-center gap-2">
                <span className="font-mono text-sm text-gray-900 dark:text-white">{c.client_id}</span>
                <span className="text-sm text-gray-500 dark:text-gray-400">{c.label}</span>
                <Badge color="gray">{t(`integrations.credentials.routes.${c.route}`)}</Badge>
                {stateBadge(c)}
                <span className="ml-auto flex gap-2">
                  <button className="btn-secondary text-xs" disabled={rotate.isPending}
                          onClick={() => setConfirming(c.client_id)}>
                    {t('integrations.credentials.rotate')}
                  </button>
                  {!c.revoked_at && (
                    <button className="btn-secondary text-xs" disabled={revoke.isPending}
                            onClick={() => revoke.mutate(c.client_id)}>
                      {t('integrations.credentials.revoke')}
                    </button>
                  )}
                </span>
              </div>
              <p className="mt-1 text-xs text-gray-500 dark:text-gray-400">
                {t('integrations.credentials.lastSeen')}: {fmt(c.last_authenticated_at)}
                {' · '}
                {t('integrations.credentials.created')}: {fmt(c.created_at)}
              </p>
              {confirming === c.client_id && (
                <div className="mt-2 rounded-lg border border-amber-300 bg-amber-50 p-3 dark:border-amber-700 dark:bg-amber-900/20">
                  {/* Blast radius, named. A bare Rotate button is a footgun: the
                      client keeps presenting the old token until updated. */}
                  <p className="text-xs text-amber-800 dark:text-amber-300">
                    {t('integrations.credentials.rotateWarning', { id: c.client_id })}
                  </p>
                  <div className="mt-2 flex gap-2">
                    <button className="btn-primary text-xs" onClick={() => doRotate(c.client_id)}>
                      {t('integrations.credentials.rotateConfirm')}
                    </button>
                    <button className="btn-secondary text-xs" onClick={() => setConfirming(null)}>
                      {t('common.cancel')}
                    </button>
                  </div>
                </div>
              )}
            </li>
          ))}
        </ul>
      )}

      {/* Legacy shared tokens */}
      {legacy.some((l) => l.configured) && (
        <div className="mt-5 border-t border-gray-200 pt-4 dark:border-gray-700">
          <h4 className="text-sm font-medium text-gray-700 dark:text-gray-300">
            {t('integrations.credentials.legacyTitle')}
          </h4>
          <ul className="mt-2 space-y-1">
            {legacy.filter((l) => l.configured).map((l) => (
              <li key={l.route} className="flex flex-wrap items-center gap-2 text-sm">
                <Badge color="gray">{t(`integrations.credentials.routes.${l.route}`)}</Badge>
                <span className="text-gray-500 dark:text-gray-400">
                  {l.source === 'env'
                    ? t('integrations.credentials.legacyEnvManaged')
                    : t('integrations.credentials.legacyDbManaged')}
                </span>
              </li>
            ))}
          </ul>
          <p className="mt-2 text-xs text-gray-500 dark:text-gray-400">
            {t('integrations.credentials.legacyNote')}
          </p>
        </div>
      )}
    </div>
  );
}
