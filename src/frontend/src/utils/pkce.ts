// PKCE + state helpers for the SSO one-time-code hand-off (token-in-URL
// replacement). The SPA generates a `code_verifier` before starting a federated
// login, sends only its S256 `code_challenge` outward, and later proves
// possession of the verifier when exchanging the one-time code for tokens — so a
// code leaked via URL/history is useless to anyone else.
//
// Verifier + state live in sessionStorage (per-tab, cleared on exchange), NEVER
// in localStorage and NEVER in a URL. See docs/design/sso-token-handoff-hardening.md.

const VERIFIER_KEY = 'renfield_pkce_verifier';
const STATE_KEY = 'renfield_pkce_state';

function base64UrlEncode(bytes: Uint8Array): string {
  let bin = '';
  for (const b of bytes) bin += String.fromCharCode(b);
  return btoa(bin).replace(/\+/g, '-').replace(/\//g, '_').replace(/=+$/, '');
}

function randomBase64Url(nBytes: number): string {
  const bytes = new Uint8Array(nBytes);
  crypto.getRandomValues(bytes);
  return base64UrlEncode(bytes);
}

/** A high-entropy code_verifier (RFC 7636 requires 43-128 chars). 64 bytes → ~86. */
export function generateVerifier(): string {
  return randomBase64Url(64);
}

/** An opaque CSRF/state nonce binding the callback to this tab. */
export function generateState(): string {
  return randomBase64Url(16);
}

/** code_challenge = base64url(SHA-256(verifier)), no padding (S256 method). */
export async function challengeFromVerifier(verifier: string): Promise<string> {
  const digest = await crypto.subtle.digest('SHA-256', new TextEncoder().encode(verifier));
  return base64UrlEncode(new Uint8Array(digest));
}

/** Stash the verifier + state before redirecting to the identity provider. */
export function storePkce(verifier: string, state: string): void {
  sessionStorage.setItem(VERIFIER_KEY, verifier);
  sessionStorage.setItem(STATE_KEY, state);
}

/** Read back the stashed verifier + state at the callback (null if absent). */
export function readPkce(): { verifier: string | null; state: string | null } {
  return {
    verifier: sessionStorage.getItem(VERIFIER_KEY),
    state: sessionStorage.getItem(STATE_KEY),
  };
}

/** Clear the stash once the exchange completes (success or failure). */
export function clearPkce(): void {
  sessionStorage.removeItem(VERIFIER_KEY);
  sessionStorage.removeItem(STATE_KEY);
}

/**
 * Begin a PKCE login: generate + stash a verifier/state and return the params
 * to hand to the backend `/sso/start` (or an authorize URL). Callers redirect.
 */
export async function beginPkceLogin(): Promise<{ challenge: string; state: string }> {
  const verifier = generateVerifier();
  const state = generateState();
  storePkce(verifier, state);
  return { challenge: await challengeFromVerifier(verifier), state };
}
