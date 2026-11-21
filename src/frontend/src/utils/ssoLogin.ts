// Redirect-provider login from the login page (`POST /api/auth/sso/start`) and
// the "which provider did this session start with" marker that decides the
// IdP logout after the renfield logout.
//
// The marker is a provider ID, never a token: it only says which
// `logout_url` from `/api/auth/status` applies. Pending (between the click and
// the callback) it lives in sessionStorage, per tab like the PKCE verifier;
// once the exchange succeeds it moves to localStorage so a session that
// outlives the tab still knows how it began. A password login clears it.

import apiClient from './axios';
import { beginPkceLogin } from './pkce';
import { leavePage } from './leavePage';

const PENDING_KEY = 'renfield_sso_pending_provider';
const LOGIN_PROVIDER_KEY = 'renfield_login_provider';

export interface SsoProvider {
  id: string;
  display_name: string;
  logout_url: string | null;
}

export type PasswordFormMode = 'visible' | 'collapsed';

/**
 * Start a redirect login: stash the SPA's PKCE verifier + state, ask the
 * backend for the IdP URL (same-origin JSON XHR, never a top-level GET), then
 * navigate there. Throws when the backend refuses or answers without a URL —
 * the caller shows the error; nothing has left the page yet.
 */
export async function startSsoLogin(providerId: string): Promise<void> {
  const { challenge, state } = await beginPkceLogin();
  const resp = await apiClient.post(
    '/api/auth/sso/start',
    { provider: providerId, code_challenge: challenge, state },
    { headers: { 'Content-Type': 'application/json' } },
  );
  const url = (resp.data as { authorize_url?: unknown }).authorize_url;
  if (typeof url !== 'string' || !url) {
    throw new Error('sso start: no authorize_url');
  }
  sessionStorage.setItem(PENDING_KEY, providerId);
  leavePage(url);
}

/** At the callback, after a successful exchange: the session began with the pending provider. */
export function promotePendingLoginProvider(): void {
  const pending = sessionStorage.getItem(PENDING_KEY);
  sessionStorage.removeItem(PENDING_KEY);
  if (pending) localStorage.setItem(LOGIN_PROVIDER_KEY, pending);
  else localStorage.removeItem(LOGIN_PROVIDER_KEY);
}

/** A failed callback: forget the pending provider. */
export function clearPendingLoginProvider(): void {
  sessionStorage.removeItem(PENDING_KEY);
}

/** A password login: this session did not begin at an IdP. */
export function clearLoginProvider(): void {
  localStorage.removeItem(LOGIN_PROVIDER_KEY);
}

/**
 * Consume the marker at logout and return where to send the browser next:
 * the matching provider's `logout_url`, or null (stay in the app). The marker
 * is cleared either way.
 */
export function takeIdpLogoutUrl(providers: SsoProvider[]): string | null {
  const id = localStorage.getItem(LOGIN_PROVIDER_KEY);
  localStorage.removeItem(LOGIN_PROVIDER_KEY);
  if (!id) return null;
  const url = providers.find((p) => p.id === id)?.logout_url;
  return typeof url === 'string' && url ? url : null;
}
