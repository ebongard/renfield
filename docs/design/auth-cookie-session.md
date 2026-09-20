# Auth: pluggable provider registry + HttpOnly-cookie session with CSRF

Long form for two auth seams that used to be described only in `CLAUDE.md`: the pluggable
login-provider registry (ebongard/renfield#591) and the JWT HttpOnly-cookie session with CSRF
(`AUTH_COOKIE_ENABLED`, dark). The must-not-break invariants are condensed in `.claude/rules/auth.md`.

Related: `docs/design/sso-token-handoff-hardening.md` (one-time code + PKCE hand-off),
`docs/design/browser-voice-auth-on-instances.md` (voice-WS token, xidra browser voice),
`docs/runbooks/cookie-auth-flag-flip-xidra.md` (enabling the cookie session on xidra),
`docs/ENVIRONMENT_VARIABLES.md` (all env vars).

## Background moved from CLAUDE.md (2026-09-20)

### Pluggable auth provider registry (ebongard/renfield#591)

`/auth/login` no longer hard-codes "an `authenticate` hook then bcrypt". It delegates to
`auth/login_flow.py::resolve_login`, which:

1. still honors the legacy `authenticate` hook first — a plugin returning a `User` is authoritative
   (the unchanged backward-compat seam);
2. then runs the **provider registry** credential walk (`auth/registry.py`): providers ordered by
   ascending `priority`, multi-active, per-provider `enabled` gate, **first non-None wins**. A provider
   that raises or exceeds `auth_provider_timeout_seconds` is **skipped fail-open** — WARNING log +
   `auth_provider_unreachable_total{provider_id}` counter + continue the walk;
3. on a `ProviderResult`, fires the **single `post_authenticate` hook** exactly once *before* the JWT
   is minted.

The cross-repo contract is `auth/provider_contract.py` (`ProviderResult` frozen dataclass +
`AuthProvider` Protocol + `PROVIDER_RESULT_CONTRACT_VERSION`). Renfield owns authn; the Reva consumer
(a `post_authenticate` handler) owns identity resolution and returns the renfield user id.

**Standalone fallback:**

- 0 handlers registered → the JWT is minted from the `db` provider's subject (legacy behavior, keeps
  the renfield test suite green).
- ≥1 handler registered but none resolve → login denied (no half-bound token).
- JWT `sub` is unchanged; the cosmetic `username` claim now carries `display_name` (no consumer reads
  it).

**Built-ins (`auth/providers/`):**

- `db` — priority 100, always on, wraps bcrypt.
- `ldap` — priority 50, authn-only: no local-user create (that is the identity consumer's job);
  config-gated.
- `google` / `github` / `apple` — redirect providers, **`enabled=False` by default**; enabling is
  config-only.

The group→role **authz seam is defined in docstrings only**, not implemented in this delivery
(`extras["ldap_member_of"]` is carried but unused).

New config: `ldap_auth_*`, `oauth_{google,github,apple}_*`, `auth_provider_timeout_seconds`.

### JWT HttpOnly-cookie session + CSRF (`AUTH_COOKIE_ENABLED`, dark)

**Why.** The session moves off `localStorage` — where any XSS steals the 30-day refresh token — into
an **HttpOnly+Secure+SameSite=Lax cookie** JS can never read, plus a stateless **double-submit CSRF**
token. **Flag off (default) → byte-identical** to the localStorage-Bearer model. The household is
auth-off (inert); the real target is xidra (auth-on). Fully reversible per flag.

**Backward-compat seam.** A **cookie-first-then-Bearer dual-read** keeps the Reva
fragment→localStorage→Bearer path, the voice-server (`internal_auth.verify`, body+header,
cookie-inert) and any legacy client working while cookies are on.

- **Reader:** `services/auth_service.py` — `oauth2_scheme` is rebound to `_cookie_or_bearer_token`
  (the cookie wins when present, else the `Authorization` header), so all five auth deps become
  cookie-aware with zero call-site edits.
- **Issuers set cookies too** (in addition to the unchanged JSON body): `api/routes/auth.py`
  `_set_auth_cookies` on login / refresh / change-password / sso-exchange; `_clear_auth_cookies` on
  logout. Refresh **dual-reads** the refresh token (cookie `Path=/api/auth/refresh` → body).
  Rotation / reuse-detection / blacklist / `token_epoch` are all unchanged — they key on the decoded
  JWT, not its transport.
- **CSRF:** `main.py` `CSRFMiddleware` (inner of CORS) enforces `X-CSRF-Token` == the JS-readable
  `renfield_csrf` cookie (constant-time) ONLY on cookie-authed mutating requests. A Bearer request
  (no auth cookie) is structurally CSRF-immune → exempt, which also covers first-login, the refresh
  bootstrap, and `/api/internal/*` (server-to-server, exempt by prefix).
- **WebSocket:** `services/websocket_auth.py` Strategy-0 reads `websocket.cookies` — a browser
  auto-sends the cookie on the same-origin WS handshake, so no long-lived JWT sits in the URL. This is
  safe because the `_ws_origin_allowed` CSWSH allowlist runs first. The `/api/ws/token` faucet stays.
  (The voice WS uses a separate `scope:"voice"` faucet token — see
  `docs/design/browser-voice-auth-on-instances.md`.)
- **Frontend:** `utils/axios.ts` sets `withCredentials:true` + a CSRF header interceptor (the Bearer
  interceptor is kept for the Reva path). `context/AuthContext.tsx` learns cookie-mode from
  `/api/auth/status` (`auth_cookie_enabled`) and, when on, discovers "logged in?" via `/me` (the
  cookie isn't JS-readable) and refreshes via the HttpOnly cookie instead of gating on a localStorage
  token. In cookie mode `AuthContext.setTokens` persists NEITHER token to localStorage.
- **Validator hard-fails** (`config.py`): `AUTH_COOKIE_ENABLED=true` with `CORS_ORIGINS='*'`
  (credentialed CORS impossible + WS CSWSH bypass), with `AUTH_ENABLED=false`, or with
  `COOKIE_SECURE=false` on a prod/staging env.

**Single auth flag.** `auth_enabled` (`AUTH_ENABLED`) is the single auth flag for REST and WebSocket;
the separate `WS_AUTH_ENABLED` is retired (see `docs/ENVIRONMENT_VARIABLES.md`).

**Deferred (own follow-ups):**

- removing the SSO fragment handler / `?code=` emitter wiring (= the SSO cutover, needs Reva);
- retiring `/api/ws/token` + the Bearer interceptor;
- flipping `FEATURE_VOICE` on xidra (runbook: `docs/design/browser-voice-auth-on-instances.md`).

**Not touched:** `SECRET_KEY` — tri-purpose (JWT + Fernet-at-rest + BLE IRK); no split, no rotation.

Env: `AUTH_COOKIE_ENABLED` / `AUTH_COOKIE_NAME` / `REFRESH_COOKIE_NAME` / `CSRF_COOKIE_NAME` /
`COOKIE_SECURE` / `COOKIE_SAMESITE` / `COOKIE_DOMAIN` (`docs/ENVIRONMENT_VARIABLES.md`).
