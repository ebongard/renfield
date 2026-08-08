# SSO token hand-off hardening — replace URL-fragment tokens with a one-time code exchange

Status: **DESIGN — not yet implemented.** Dark by default at rollout.
Owners: auth. Related: `auth/provider_contract.py`, `auth/registry.py`,
`api/routes/auth.py`, `src/frontend/src/main.tsx`, the pluggable auth-provider
registry (CLAUDE.md → "Pluggable auth provider registry").

## 1. Problem

After a federated login (Entra/OIDC today; Google/GitHub/Apple when enabled) the
backend hands the minted application JWT back to the SPA by **redirecting into
the URL fragment**:

```
https://<host>/#access_token=<JWT>&expires_in=<seconds>&provider=entra
```

`src/frontend/src/utils/ssoFragment.ts::consumeSsoFragmentHandoff()` (called from
`main.tsx`, extracted from the former `_consumeOidcHashHandoff()`) reads that
fragment, writes the JWT to `localStorage`, and scrubs the fragment with
`history.replaceState`. This is the OAuth 2.0 **implicit flow**, deprecated by
the OAuth Security BCP (RFC 9700 / draft-ietf-oauth-security-topics) precisely
because **the access token travels in a URL**. The fragment is never sent to the
server, so it avoids HTTP-access-log leakage — but that is the *only* leak vector
it closes. It remains exposed to:

- **Browser history** — the full URL (fragment included) is written to history
  before `replaceState` runs, and on some browsers/extensions survives the scrub.
- **Referrer / navigation** — any synchronous script or `<meta refresh>` racing
  the handler can read `location.hash`.
- **Browser extensions & shared devices** — anything with `tabs`/`history`
  permission or a shoulder-surfer sees the token in the address bar momentarily.
- **Token injection / fixation (the sharpest one).** An attacker who lures a
  victim to `https://<host>/#access_token=<attacker-controlled-JWT>` fixes the
  victim's session to a token the attacker knows — there is **no binding**
  between the token and a login this browser actually initiated.

On the current household + xidra instances no federated provider is enabled, so
the handler is **dormant attack surface** (a token-injection sink with no
legitimate producer). In the pro/Reva edition the OIDC callback genuinely uses
it. Either way the mechanism, not just its exposure, must be replaced.

**Interim hardening (security audit, shipped ahead of the full cutover).**
`consumeSsoFragmentHandoff()` is now (a) gated behind the `VITE_SSO_LEGACY_FRAGMENT`
build flag (kill switch — a post-cutover build sets it `false` and the handler is
removed), (b) restricted to storing only a structurally valid, **unexpired
`type:"access"` JWT** (`looksLikeUnexpiredAccessJwt`) — the browser can't verify
the HS256 signature, so this narrows but does not fully close the injection sink;
full closure still requires the `?code=`+PKCE cutover below — and (c) it **always**
strips the fragment, even when it rejects the token. Covered by
`tests/frontend/react/utils/ssoFragment.test.ts`.

## 2. Goals / non-goals

**Goals**
- A federated login token **never appears in a URL** (fragment or query).
- The hand-off is **bound to a login this browser initiated** (kills injection/fixation).
- Keep the rest of the app unchanged: Bearer-in-`localStorage`, the axios
  interceptor, WS `?token=` handshake, kiosk — all keep working.
- One mechanism that serves both the Reva/entra emitter and Renfield's own
  (currently-dark) google/github/apple redirect providers.
- Backward-compatible, flag-gated rollout with a clean cutover.

**Non-goals (explicitly deferred)**
- Moving to HttpOnly-cookie session auth (would rewrite axios + WS + kiosk auth
  and add CSRF plumbing — tracked as a separate, larger initiative; see §9).
- Changing how the backend talks to the IdP (the server-side OIDC code exchange
  with Entra/Google is unchanged; we only harden the **backend→SPA** leg).

## 3. Chosen design — one-time authorization code + PKCE on the backend→SPA leg

A **Backend-for-Frontend (BFF) one-time-code exchange**. The backend still
completes the OIDC dance server-side and mints the application session, but
instead of *embedding* that session in the redirect URL it stores it under a
**single-use, short-TTL code** and redirects with only that opaque code. The SPA
**POSTs** the code back and receives the tokens **in the response body over
TLS**. PKCE binds the code to the specific browser that started the login.

### 3.1 Sequence

```
SPA                              Backend (authn)                     IdP
 |  1. start login                    |                                |
 |  gen code_verifier (128B random)   |                                |
 |  code_challenge = S256(verifier)   |                                |
 |  state = random; store {verifier,  |                                |
 |    state} in sessionStorage        |                                |
 |  GET /api/auth/sso/start?provider= |                                |
 |     &code_challenge=&state= ------> |  stash challenge under state   |
 |                                     |  302 to IdP authorize -------> |
 |                                     |                                |  user authenticates
 |                                     | <---- 302 ?code_idp=&state ----|
 |                                     |  server-side token exchange    |
 |                                     |  + resolve_login()/            |
 |                                     |  post_authenticate() → app JWT |
 |                                     |  mint one_time_code, store      |
 |                                     |  {app_jwt, refresh, exp} bound  |
 |                                     |  to (state, code_challenge),    |
 |                                     |  TTL 60s, single-use            |
 | <-- 302 /auth/callback?code=&state=|                                |
 |  2. /auth/callback route:          |                                |
 |  verify state == stored            |                                |
 |  POST /api/auth/sso/exchange       |                                |
 |    {code, code_verifier, state} -->|  load by code (atomic GETDEL)   |
 |                                     |  verify S256(verifier)==        |
 |                                     |    stored challenge; verify     |
 |                                     |    state; check not expired     |
 | <-- 200 {access_token, refresh_    |  delete code (single-use)       |
 |     token, expires_in} (body)      |                                |
 |  store tokens in localStorage      |                                |
 |  clear sessionStorage; go to app   |                                |
```

The token crosses exactly once, in step 2's **POST response body**. The URL only
ever carries `code` + `state`, both opaque and single-use.

### 3.2 Why PKCE here, when the backend already did the IdP exchange

The IdP leg is already server-side (confidential client). PKCE is added on the
**SPA↔backend** leg so the one-time `code` in the redirect URL is worthless to
anyone but the browser holding the matching `code_verifier`. Without it, a leaked
`code` (history/extension) could be raced to `/exchange` by an attacker before
the victim. With S256 PKCE, the attacker has the code but not the verifier, and
the exchange fails. `state` additionally binds the callback to the initiating
tab and defends CSRF on the callback.

## 4. Backend components (Renfield)

All new surface lives beside the existing provider registry so both Renfield's
own redirect providers and the Reva emitter share it.

1. **One-time code store** — `services/sso_handoff_store.py`, backed by Redis
   (already in-cluster). Key `sso:handoff:<code>` → JSON `{user_id,
   code_challenge, state, provider}` — a session **reference, NOT the tokens**
   (the tokens are minted at exchange time, §2, so no JWT ever sits in Redis and a
   ≤TTL Redis peek yields no usable session). `SET … EX 60 NX` (the write is
   verified — a collision raises rather than redirecting with an un-stored code);
   read via **`GETDEL`** (atomic single-use — a replayed code finds nothing).
   `code` = 256-bit URL-safe random. No DB table (ephemeral, self-expiring).

2. **`POST /api/auth/sso/exchange`** (`api/routes/auth.py`) — body `{code,
   code_verifier, state}`. Steps: `GETDEL` the code (opaque 400 on miss/used);
   constant-time `sha256_b64url(code_verifier) == code_challenge` (verifier is
   RFC-7636 charset-validated, so a non-ASCII verifier is a clean 400, not a 500);
   constant-time `state`; re-load + re-validate the user (`is_active`); **mint the
   access/refresh JWTs now** and return `TokenResponse{access_token,
   refresh_token, expires_in, must_change_password}` — **the same shape `/login`
   already returns**, with a current `must_change_password`. Rate-limited
   (`api_rate_limit_auth`).

3. **Code issuance at the OIDC callback.** Wherever the ProviderResult →
   `post_authenticate` → JWT mint completes (Renfield-side for its own providers;
   Reva-side for entra — see §6), replace "redirect with `#access_token=`" with
   "store handoff, redirect `…/auth/callback?code=<code>&state=<state>`". The mint
   path (`create_access_token`/`create_refresh_token`) is unchanged.

4. **`GET /api/auth/sso/start`** (optional, if we want the challenge stashed
   server-side under `state` rather than only in the SPA). Accepts `provider`,
   `code_challenge`, `state`; 302s to the provider `authorize_url`. Lets the
   backend bind the challenge to the login before the IdP round-trip.

5. **Config:** `sso_handoff_ttl_seconds` (default 60), `sso_handoff_enabled`
   (rollout flag). No secrets.

## 5. Frontend components

1. **New route `/auth/callback`** (`pages/AuthCallback.tsx`): reads `code` +
   `state` from the **query string** (not fragment); verifies `state` against
   `sessionStorage`; POSTs `{code, code_verifier, state}` to
   `/api/auth/sso/exchange`; on success calls the existing
   `AuthContext.setTokens(...)` and navigates to the stored `from` path; on
   failure routes to `/login?error=sso`.
2. **PKCE/state helper** (`utils/pkce.ts`): `generateVerifier()` (128-byte random
   → base64url), `challenge(verifier)` (WebCrypto `SHA-256` → base64url),
   `randomState()`. Verifier/state live in `sessionStorage` (per-tab, cleared on
   exchange) — never `localStorage`, never a URL.
3. **Login initiation**: the "Sign in with …" buttons call
   `/api/auth/sso/start?provider=…&code_challenge=…&state=…` (or build the
   authorize URL client-side and pass the challenge) instead of linking straight
   to the IdP.
4. **Delete `_consumeOidcHashHandoff()`** from `main.tsx` once the flag is on
   everywhere (see §7). Nothing else reads `#access_token=`.

## 6. Cross-repo contract (Reva/entra emitter)

Renfield owns authn and now owns the **hand-off contract**; the Reva edition's
OIDC callback is the emitter today. Contract (versioned alongside
`PROVIDER_RESULT_CONTRACT_VERSION`):

- The emitter, after resolving identity and minting (or obtaining) the app
  session, MUST call the shared handoff-store issue step and redirect to
  `…/auth/callback?code=<opaque>&state=<state>` — **never** `#access_token=`.
- If the emitter lives in a different process than the code store, expose the
  issue step as an internal authenticated call, or have the emitter redirect to a
  Renfield `…/sso/finish` endpoint that performs the store+redirect. (Decision
  point flagged in §10 — depends on where the Reva callback runs relative to
  Renfield's Redis.)
- Until the emitter is updated, the legacy fragment path stays available behind
  the flag so pro SSO does not break mid-migration.

## 7. Rollout (no flag-day break)

1. Ship the backend exchange endpoint + code store + `/auth/callback` route, all
   behind `sso_handoff_enabled=false`. The old fragment handler stays. Byte-identical when off.
2. Enable on an instance that actually uses federated login; update that
   instance's emitter (Renfield providers) / coordinate the Reva emitter.
3. Verify end-to-end (E2E in §8). Watch for `sso_exchange_failure` metric.
4. Once every emitter emits `?code=`, **remove `_consumeOidcHashHandoff()`** and
   the fragment branch — the token-in-URL sink is gone for good.
5. Household/xidra (no SSO) can jump straight to step 4 after the emitter audit —
   removing dormant surface with zero functional change.

## 8. Testing

- **Backend unit** (`tests/backend/test_sso_handoff.py`): issue→exchange happy
  path; replay of a used code → 400 (GETDEL atomicity); wrong `code_verifier` →
  400; wrong `state` → 400; expired code → 400; exchange returns the same
  `TokenResponse` shape as `/login`.
- **Frontend** (`tests/frontend/react/pages/AuthCallback.test.tsx`): state
  mismatch → error route; successful exchange stores tokens via `setTokens`;
  verifier pulled from sessionStorage and cleared after.
- **Browser E2E** (`tests/e2e/areas/test_sso_handoff.py`, auth-on target): drive a
  stub provider through start→callback→exchange, assert the address bar never
  contains a JWT at any step and the session lands authenticated. Reuses the
  env-sourced-credential harness added for Notes (no secret in URL/transcript).

## 9. Alternatives considered

- **Remove the fragment handler, Renfield-only.** Closes the dormant sink here
  with zero loss, but the same frontend built as `pro` serves Reva, whose OIDC
  relies on it — so removal alone breaks pro SSO. Folded into §7 step 4/5 as the
  *end state* once the emitter is migrated, not a standalone fix.
- **HttpOnly Secure cookie session.** Strongest (token never in JS at all; also
  kills XSS token theft). Rejected for *this* change as too broad: it rewrites the
  axios Bearer interceptor, the WS `?token=` handshake, and kiosk auth, and adds
  CSRF tokens across every mutating route. Worth doing later; this design does not
  block it (the exchange endpoint could set a cookie instead of returning a body
  as a future variant).

## 10. Open questions

1. **Where does the Reva OIDC callback run** relative to Renfield's Redis? Decides
   whether the emitter calls the code store directly or via a Renfield
   `…/sso/finish` redirect (§6).
2. **Refresh-token delivery**: return it in the exchange body (as today's login
   does) or move refresh to an HttpOnly cookie now as a first step toward §9?
3. Do we keep `GET /api/auth/sso/start` (server-stashed challenge) or let the SPA
   build the authorize URL and hold the challenge itself? Server-stashed is
   slightly stronger (challenge bound before the IdP hop) at the cost of one more
   endpoint.
