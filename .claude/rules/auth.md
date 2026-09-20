---
paths:
  - "src/backend/auth/**"
  - "src/backend/services/auth_service.py"
  - "src/backend/services/sso_handoff_store.py"
  - "src/backend/services/websocket_auth.py"
  - "src/backend/api/routes/auth.py"
  - "src/backend/api/routes/internal_auth.py"
  - "src/frontend/src/context/AuthContext.tsx"
  - "src/frontend/src/utils/axios.ts"
  - "src/frontend/src/pages/AuthCallback.tsx"
  - "src/frontend/src/pages/ChatPage/hooks/useVoiceStream.ts"
---
# Auth: login flow, SSO hand-off, cookie session, voice token

Loaded only when an auth file is read. Long form: `docs/design/auth-cookie-session.md`,
`docs/design/sso-token-handoff-hardening.md`, `docs/design/browser-voice-auth-on-instances.md`,
`docs/runbooks/cookie-auth-flag-flip-xidra.md`, env list `docs/ENVIRONMENT_VARIABLES.md`.

## Flags
- **`AUTH_ENABLED` is the SINGLE auth flag** (REST + WebSocket). `WS_AUTH_ENABLED` is retired; a leftover that
  CONTRADICTS `AUTH_ENABLED` fails boot (a matching leftover only warns).
- `SSO_HANDOFF_ENABLED` dark · `AUTH_COOKIE_ENABLED` dark — flag off stays byte-identical to the localStorage-Bearer model.
- **`SECRET_KEY` is tri-purpose (JWT + Fernet-at-rest + BLE IRKs). Rotation is DESTRUCTIVE — never split/rotate casually.**

## Login flow (`auth/login_flow.py::resolve_login`, called by `/auth/login`)
1. The legacy `authenticate` hook first — a plugin returning a `User` is authoritative.
2. Provider registry walk (`auth/registry.py`): ascending `priority`, multi-active, per-provider `enabled` gate,
   **first non-None wins**. A provider that raises or exceeds `auth_provider_timeout_seconds` is skipped **FAIL-OPEN**:
   WARNING + `auth_provider_unreachable_total{provider_id}` + continue the walk.
3. On a `ProviderResult` the single `post_authenticate` hook fires **exactly once, BEFORE the JWT is minted**.
- 0 handlers → JWT from the `db` provider's subject. ≥1 handler but none resolves → login **DENIED** (no half-bound token).
- Cross-repo contract = `auth/provider_contract.py` (`ProviderResult`, `AuthProvider`, `PROVIDER_RESULT_CONTRACT_VERSION`).
  Built-ins: `db` (priority 100, always on), `ldap` (50, authn-only — never creates a local user),
  `google`/`github`/`apple` (`enabled=False` by default, enabling is config-only).

## SSO hand-off (`services/sso_handoff_store.py`, `POST /api/auth/sso/exchange`)
Single-use 60 s Redis code = a session *reference*, bound to an S256 `code_challenge` + `state`; burned by atomic
`GETDEL`; PKCE + state compared constant-time; the user is re-validated and the JWTs are **minted at EXCHANGE time** —
no token ever in Redis or a URL. 404 when the flag is off. Redirects carry only `?code=&state=`.

## Cookie session + CSRF
- Reader: `oauth2_scheme` is rebound to `_cookie_or_bearer_token` — **cookie first, then the `Authorization` header**
  (all five auth deps). Keep the dual read: the Bearer path and the voice-server (`internal_auth.verify`) rely on it.
- Issuers: `_set_auth_cookies` on login/refresh/change-password/sso-exchange, `_clear_auth_cookies` on logout; the JSON
  body stays. Refresh dual-reads (cookie → body). Rotation/reuse-detection/blacklist/`token_epoch` key on the JWT.
- `CSRFMiddleware` (`main.py`, inner of CORS): `X-CSRF-Token` == the `renfield_csrf` cookie, constant-time, enforced
  **ONLY on cookie-authed mutating requests**. A Bearer request (no auth cookie) is structurally immune → exempt; that
  covers first login, the refresh bootstrap and `/api/internal/*` (exempt by prefix).
- WS Strategy-0 reads `websocket.cookies` — safe **ONLY because the `_ws_origin_allowed` CSWSH allowlist runs first**.
- Validator hard-fails (`config.py`): `AUTH_COOKIE_ENABLED=true` with `CORS_ORIGINS='*'`, with `AUTH_ENABLED=false`, or
  with `COOKIE_SECURE=false` on a prod/staging env.
- Frontend cookie mode: `AuthContext.setTokens` persists NEITHER token to localStorage; "logged in?" comes from `/me`.

## Voice token + voiceprint privacy gate
- `/api/ws/token?purpose=voice` mints a short-lived `scope:"voice"` token for the external voice-server only; its verify
  path (`/api/internal/auth/verify`) accepts any non-`ws` scope. **REST and renfield's own `/ws/*` REJECT `scope:voice`.**
- **With `SPEAKER_RECOGNITION_ENABLED=false` NO voiceprint is persisted on ANY path (Art. 9 GDPR):**
  `chat_handler._resolve_wire_speaker` + `speaker_resolver.resolve_speaker_from_embedding` (refuse before DB access),
  enrollment routes/service (409), `Meeting.segments` (`meeting_pipeline.strip_biometric_fields`, always), fingerprints.
