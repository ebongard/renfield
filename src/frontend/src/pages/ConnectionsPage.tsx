/**
 * Connections — per-user tool credentials (per-user data scoping).
 *
 * Lists connectable tools; the user pastes a per-user token so Reva acts as
 * them (not a shared account). Secrets are write-only — the list only shows a
 * connected/not-connected status. See docs/architecture/connections-ui-spec.md.
 */
import { useState } from 'react';
import { useTranslation } from 'react-i18next';
import { Plug, ExternalLink, Loader2, CheckCircle2, Circle, KeyRound } from 'lucide-react';
import PageHeader from '../components/PageHeader';
import Modal from '../components/Modal';
import Badge from '../components/Badge';
import Alert from '../components/Alert';
import {
  useConnections,
  useConnect,
  useDisconnect,
  isSsoProvider,
  type ConnectionProvider,
} from '../api/resources/connections';

export default function ConnectionsPage() {
  const { t } = useTranslation();
  const { data: providers, isLoading, isError, errorMessage } = useConnections();
  const connect = useConnect();
  const disconnect = useDisconnect();

  // modal state: the provider being connected/managed, and the paste field.
  // `sso` is its own mode — those connections have no token to paste and no
  // token to revoke, so both the paste form and the disconnect action would
  // only lead to a server-side refusal.
  const [active, setActive] = useState<ConnectionProvider | null>(null);
  const [mode, setMode] = useState<'connect' | 'manage' | 'sso'>('connect');
  const [secret, setSecret] = useState('');

  const open = (p: ConnectionProvider) => {
    setActive(p);
    setMode(isSsoProvider(p) ? 'sso' : p.connected ? 'manage' : 'connect');
    setSecret('');
    connect.reset?.();
  };
  const close = () => {
    setActive(null);
    setSecret('');
  };

  const submit = async () => {
    if (!active || !secret.trim()) return;
    await connect.mutateAsync({ providerKey: active.provider_key, secret: secret.trim() });
    close();
  };
  const remove = async () => {
    if (!active) return;
    await disconnect.mutateAsync(active.provider_key);
    close();
  };

  return (
    <div className="max-w-3xl mx-auto">
      <PageHeader
        icon={Plug}
        title={t('connections.title')}
        subtitle={t('connections.subtitle')}
      />

      {isError && <Alert variant="error" className="mb-4">{errorMessage}</Alert>}

      {isLoading ? (
        <div className="flex items-center gap-2 text-gray-500 py-10 justify-center">
          <Loader2 className="w-4 h-4 animate-spin" aria-hidden="true" />
          {t('connections.loading')}
        </div>
      ) : (
        <div className="card divide-y divide-gray-100 dark:divide-gray-700 p-0 overflow-hidden">
          {(providers ?? []).map((p) => {
            const sso = isSsoProvider(p);
            return (
              <div key={p.provider_key} className="flex items-center gap-4 px-5 py-4">
                <div className="flex-1 min-w-0">
                  <div className="font-semibold text-gray-900 dark:text-gray-100">
                    {p.display_name ?? p.provider_key}
                  </div>
                  {p.descriptor && (
                    <div className="text-sm text-gray-500 dark:text-gray-400 truncate">
                      {p.descriptor}
                    </div>
                  )}
                  {sso && (
                    // Say up front that this one works differently, so an
                    // unconnected state doesn't read as "go find a token".
                    <div className="text-xs text-gray-400 dark:text-gray-500 mt-0.5 inline-flex items-center gap-1">
                      <KeyRound className="w-3 h-3" aria-hidden="true" />
                      {t('connections.viaSso')}
                    </div>
                  )}
                </div>
                {p.connected ? (
                  <Badge color="green" icon={CheckCircle2}>{t('connections.status.connected')}</Badge>
                ) : (
                  <Badge color="gray" icon={Circle}>{t('connections.status.notConnected')}</Badge>
                )}
                <button
                  type="button"
                  onClick={() => open(p)}
                  className={sso || p.connected ? 'btn-secondary' : 'btn-primary'}
                >
                  {sso
                    ? t('connections.details')
                    : p.connected
                      ? t('connections.manage')
                      : t('connections.connect')}
                </button>
              </div>
            );
          })}
          {(providers ?? []).length === 0 && (
            <div className="px-5 py-10 text-center text-gray-500 dark:text-gray-400">
              {t('connections.empty')}
            </div>
          )}
        </div>
      )}

      {/* Connect / manage modal */}
      <Modal
        isOpen={active !== null}
        onClose={close}
        title={
          active
            ? t(
                mode === 'manage'
                  ? 'connections.manageTitle'
                  : mode === 'sso'
                    ? 'connections.ssoTitle'
                    : 'connections.connectTitle',
                { name: active.display_name ?? active.provider_key },
              )
            : ''
        }
      >
        {active && (
          <div className="space-y-4">
            {mode === 'sso' ? (
              <>
                <p className="text-sm text-gray-600 dark:text-gray-300">
                  {active.help ?? t('connections.ssoDefaultHelp')}
                </p>
                {active.connected ? (
                  <Alert variant="success">{t('connections.ssoConnected')}</Alert>
                ) : (
                  // The remedy is a fresh login, not a token hunt. Without
                  // saying so, "Not connected" on a provider with no Connect
                  // button is a dead end.
                  <Alert variant="warning">{t('connections.ssoNotConnected')}</Alert>
                )}
                <div className="flex justify-end pt-1">
                  <button type="button" className="btn-secondary" onClick={close}>
                    {t('common.close')}
                  </button>
                </div>
              </>
            ) : mode === 'connect' ? (
              <>
                <p className="text-sm text-gray-600 dark:text-gray-300">
                  {active.help ?? t('connections.defaultHelp')}
                  {active.mint_url && (
                    <>
                      {' '}
                      <a
                        href={active.mint_url}
                        target="_blank"
                        rel="noreferrer"
                        className="text-primary-600 dark:text-primary-400 font-medium inline-flex items-center gap-1"
                      >
                        {t('connections.mintLink')}
                        <ExternalLink className="w-3.5 h-3.5" aria-hidden="true" />
                      </a>
                    </>
                  )}
                </p>
                <div>
                  <label
                    htmlFor="connection-token"
                    className="block text-sm font-medium text-gray-700 dark:text-gray-300 mb-1"
                  >
                    {t('connections.tokenLabel')}
                  </label>
                  <input
                    id="connection-token"
                    type="password"
                    autoComplete="off"
                    className="input w-full"
                    value={secret}
                    onChange={(e) => setSecret(e.target.value)}
                    aria-describedby="connection-token-hint"
                  />
                  <p id="connection-token-hint" className="text-xs text-gray-500 dark:text-gray-400 mt-1.5">
                    {t('connections.tokenHint')}
                  </p>
                </div>
                {connect.errorMessage && <Alert variant="error">{connect.errorMessage}</Alert>}
                <div className="flex justify-end gap-2 pt-1">
                  <button type="button" className="btn-secondary" onClick={close}>
                    {t('common.cancel')}
                  </button>
                  <button
                    type="button"
                    className="btn-primary"
                    disabled={!secret.trim() || connect.isPending}
                    onClick={submit}
                  >
                    {connect.isPending ? t('connections.saving') : t('connections.save')}
                  </button>
                </div>
              </>
            ) : (
              <>
                <p className="text-sm text-gray-600 dark:text-gray-300">
                  {t('connections.manageBody', { name: active.display_name ?? active.provider_key })}
                </p>
                {disconnect.errorMessage && <Alert variant="error">{disconnect.errorMessage}</Alert>}
                <div className="flex justify-between gap-2 pt-1">
                  <button
                    type="button"
                    className="btn-danger"
                    disabled={disconnect.isPending}
                    onClick={remove}
                  >
                    {disconnect.isPending ? t('connections.disconnecting') : t('connections.disconnect')}
                  </button>
                  <button
                    type="button"
                    className="btn-primary"
                    onClick={() => setMode('connect')}
                  >
                    {t('connections.rotate')}
                  </button>
                </div>
              </>
            )}
          </div>
        )}
      </Modal>
    </div>
  );
}
