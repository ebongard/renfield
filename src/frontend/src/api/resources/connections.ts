/**
 * Connections — per-user tool credentials (per-user data scoping).
 *
 * Talks to Reva's /api/connections. Secrets are write-only: the list never
 * returns a token, only a `connected` boolean per provider.
 */
import { useQueryClient } from '@tanstack/react-query';
import apiClient from '../../utils/axios';
import { useApiQuery, useApiMutation } from '../hooks';
import { keys, STALE } from '../keys';

/** Credential types the catalog can declare. `sso` has nothing to paste — its
 *  credential is minted from the user's own login, so connect/disconnect are
 *  refused server-side and the UI must not offer them. */
export const SSO_CREDENTIAL_TYPE = 'sso';

export interface ConnectionProvider {
  provider_key: string;
  display_name?: string;
  descriptor?: string;
  /** 'paste_token' | 'oauth3lo' | 'sso' — see config/connections.yaml */
  credential_type?: string;
  read_only?: boolean;
  mint_url?: string;
  help?: string;
  /** For `sso` providers: the vault key the login capture writes, which is
   *  where `connected` is derived from. A provider name, never a secret. */
  sso_source?: string;
  connected: boolean;
  /** Reva's latest verdict on the stored token (paste-token providers only):
   *  'valid' | 'rejected' | 'error' | 'malformed'. Absent = never checked. */
  check_state?: CredentialCheckState | null;
  /** When the stored token expires, if the provider exposes it (ISO-8601). */
  expires_at?: string | null;
  /** When Reva last asked the provider (ISO-8601). */
  checked_at?: string | null;
  /** The tool currently reaches this user through their SSO login (OBO); a
   *  stored token is then only the fallback for a failed exchange. */
  sso_active?: boolean;
}

export type CredentialCheckState = 'valid' | 'rejected' | 'error' | 'malformed';

/** Days before expiry the page starts warning. Mirrors Reva's default
 *  REVA_CREDENTIAL_EXPIRY_WARN_DAYS, so page and chat notice agree. */
export const EXPIRY_WARN_DAYS = 14;

/** What the page should say about a stored token, derived only from the
 *  server's verdict and expiry date — never from tool output.
 *  - 'rejected': the provider refused it, it is unusable, or it is past expiry
 *  - 'expiring': still valid, expires within EXPIRY_WARN_DAYS
 *  - 'unchecked': the last check could not reach a verdict (provider error)
 *  - null: nothing to say (valid, not checked yet, or not a stored token) */
export type CredentialHealth = 'rejected' | 'expiring' | 'unchecked' | null;

export function credentialHealth(p: ConnectionProvider, now: Date = new Date()): CredentialHealth {
  if (!p.connected || isSsoProvider(p) || !p.check_state) return null;
  if (p.check_state === 'rejected' || p.check_state === 'malformed') return 'rejected';
  // The expiry date is known regardless of the last check's outcome: a
  // provider outage ('error') must not hide a token that is past its date.
  const exp = expiryTime(p);
  if (exp !== null) {
    if (exp <= now.getTime()) return 'rejected';
    if (exp - now.getTime() <= EXPIRY_WARN_DAYS * 86_400_000) return 'expiring';
  }
  return p.check_state === 'error' ? 'unchecked' : null;
}

/** `expires_at` as epoch ms, or null when absent or unparseable. */
export function expiryTime(p: ConnectionProvider): number | null {
  if (!p.expires_at) return null;
  const t = Date.parse(p.expires_at);
  return Number.isNaN(t) ? null : t;
}

/** True when this connection is established by logging in, not by pasting. */
export function isSsoProvider(p: ConnectionProvider): boolean {
  return p.credential_type === SSO_CREDENTIAL_TYPE;
}

async function fetchConnections(): Promise<ConnectionProvider[]> {
  const response = await apiClient.get<ConnectionProvider[]>('/api/connections');
  return response.data ?? [];
}

async function connectRequest(input: { providerKey: string; secret: string }): Promise<void> {
  await apiClient.put(`/api/connections/${encodeURIComponent(input.providerKey)}`, {
    secret: input.secret,
  });
}

async function disconnectRequest(providerKey: string): Promise<void> {
  await apiClient.delete(`/api/connections/${encodeURIComponent(providerKey)}`);
}

export function useConnections() {
  return useApiQuery<ConnectionProvider[]>(
    {
      queryKey: keys.connections.list(),
      queryFn: fetchConnections,
      staleTime: STALE.DEFAULT,
    },
    'connections.errors.load',
  );
}

export function useConnect() {
  const queryClient = useQueryClient();
  return useApiMutation<void, { providerKey: string; secret: string }>(
    {
      mutationFn: connectRequest,
      onSuccess: () => queryClient.invalidateQueries({ queryKey: keys.connections.all }),
    },
    'connections.errors.connect',
  );
}

export function useDisconnect() {
  const queryClient = useQueryClient();
  return useApiMutation<void, string>(
    {
      mutationFn: disconnectRequest,
      onSuccess: () => queryClient.invalidateQueries({ queryKey: keys.connections.all }),
    },
    'connections.errors.disconnect',
  );
}
