import { ACCESS_TOKEN_KEY } from './authTokens';

/**
 * Legacy OIDC URL-fragment hand-off (security audit). The old implicit flow
 * redirects to /#access_token=<JWT>; this consumes it into localStorage before
 * React mounts. It is a token-INJECTION sink (any attacker-crafted fragment is
 * copied in), which the ?code=+PKCE exchange (AuthCallback.tsx) replaces. Until
 * every emitter is migrated, this stays behind a build flag and only accepts a
 * structurally valid, unexpired access JWT.
 *
 * Extracted from main.tsx so the guard is unit-testable. The browser cannot
 * verify the HS256 signature (server secret), so this is a shape/expiry gate,
 * not authenticity — full closure comes with the ?code= cutover.
 */

/** True only for a structurally valid, unexpired `type:"access"` JWT. */
export function looksLikeUnexpiredAccessJwt(token: string): boolean {
  const parts = token.split('.');
  if (parts.length !== 3) return false;
  try {
    const payload = JSON.parse(
      atob(parts[1].replace(/-/g, '+').replace(/_/g, '/')),
    ) as { type?: string; exp?: number };
    return (
      payload.type === 'access'
      && typeof payload.exp === 'number'
      && payload.exp * 1000 > Date.now()
    );
  } catch {
    return false;
  }
}

/**
 * Consume a legacy `#access_token=<JWT>` hand-off: store a valid token in
 * localStorage and ALWAYS strip the fragment from the URL (even on rejection,
 * so a crafted value never lingers in history/Referer). No-op when `enabled`
 * is false (the kill switch for the post-`?code=`-cutover build).
 */
export function consumeSsoFragmentHandoff(enabled: boolean): void {
  if (!enabled) return;
  const hash = window.location.hash;
  if (!hash || !hash.startsWith('#access_token=')) {
    return;
  }
  const params = new URLSearchParams(hash.slice(1));
  const accessToken = params.get('access_token');
  const clearFragment = (): void => {
    history.replaceState(
      null,
      '',
      window.location.pathname + window.location.search,
    );
  };
  if (!accessToken || !looksLikeUnexpiredAccessJwt(accessToken)) {
    clearFragment();
    return;
  }
  localStorage.setItem(ACCESS_TOKEN_KEY, accessToken);
  clearFragment();
}
