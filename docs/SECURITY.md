# Security Documentation

This document describes the security measures implemented in Renfield.

## Security Headers

Both the backend (FastAPI) and frontend (Vite) implement OWASP-recommended security headers.

### Implemented Headers

| Header | Value | Purpose |
|--------|-------|---------|
| `X-Content-Type-Options` | `nosniff` | Prevents MIME type sniffing |
| `X-Frame-Options` | `DENY` | Prevents clickjacking attacks |
| `X-XSS-Protection` | `1; mode=block` | Legacy XSS protection for older browsers |
| `Referrer-Policy` | `strict-origin-when-cross-origin` | Controls referrer information |
| `Permissions-Policy` | See below | Restricts browser features |
| `Cross-Origin-Opener-Policy` | `same-origin` | Spectre/Meltdown protection |
| `Cross-Origin-Embedder-Policy` | `require-corp` | Cross-origin isolation (WASM/SharedArrayBuffer) |
| `Cross-Origin-Resource-Policy` | `same-origin` | Cross-origin isolation |
| `Content-Security-Policy` | See below | XSS and injection prevention |

### Permissions Policy

```
accelerometer=(), camera=(), geolocation=(), gyroscope=(),
magnetometer=(), microphone=(self), payment=(), usb=()
```

- Microphone is allowed for voice input functionality
- All other sensitive APIs are disabled

### Content Security Policy

```
default-src 'self';
script-src 'self' 'unsafe-inline';
style-src 'self' 'unsafe-inline';
img-src 'self' data: blob:;
font-src 'self' data:;
connect-src 'self' ws: wss:;
media-src 'self' blob:;
frame-ancestors 'none';
```

- `unsafe-inline` is required for React's inline styles and Vite-injected scripts
- WebSocket connections (`ws:`, `wss:`) are needed for chat, device, and satellite communication
- `frame-ancestors 'none'` prevents embedding (equivalent to `X-Frame-Options: DENY`)

## Dependency Security

### Automated Audits

Run security audits regularly:

```bash
# Frontend (npm)
cd src/frontend && npm audit

# Backend (pip-audit)
docker compose exec backend pip-audit
```

### Known Vulnerabilities

| Package | CVE | Status | Notes |
|---------|-----|--------|-------|
| ecdsa | CVE-2024-23342 | Won't Fix | Upstream considers timing attacks out of scope |

## OWASP ZAP Testing

Run OWASP ZAP baseline scans:

```bash
# Frontend scan
docker run --rm -t --network host ghcr.io/zaproxy/zaproxy:stable \
  zap-baseline.py -t http://localhost:3000 -I

# Backend API scan
docker run --rm -t --network host ghcr.io/zaproxy/zaproxy:stable \
  zap-baseline.py -t http://localhost:8000 -I
```

### Expected Results

- **Frontend:** 60+ PASS, 0 FAIL
- **Backend:** 64+ PASS, 0 FAIL

## Authentication & Authorization

See [ACCESS_CONTROL.md](ACCESS_CONTROL.md) for details on:
- JWT-based authentication
- Role-Permission Based Access Control (RPBAC)
- Voice authentication

## Rate Limiting

### REST API (slowapi)

Rate limiting is enabled by default (`API_RATE_LIMIT_ENABLED=true`) using slowapi with per-IP tracking.

| Endpoint Group | Default Limit | Setting |
|----------------|---------------|---------|
| Most endpoints | 100/minute | `API_RATE_LIMIT_DEFAULT` |
| Auth (login, register) | 10/minute | `API_RATE_LIMIT_AUTH` |
| Voice (STT, TTS) | 30/minute | `API_RATE_LIMIT_VOICE` |
| Chat | 60/minute | `API_RATE_LIMIT_CHAT` |
| Admin | 200/minute | `API_RATE_LIMIT_ADMIN` |

**Storage backend (`API_RATE_LIMIT_STORAGE_URI`, default `memory://`).** Counters are per-pod by default. A multi-replica deploy under-counts (each pod limits independently), so set this to the Redis URL (e.g. `${REDIS_URL}`) for shared **per-cluster** limiting once more than one backend pod runs.

### Account Lockout

Beyond the per-IP request cap, a **username** is locked after repeated failed logins (`LOGIN_LOCKOUT_ENABLED=true`), which stops credential-stuffing that rotates source IPs against one account.

| Setting | Default | Description |
|---------|---------|-------------|
| `LOGIN_LOCKOUT_ENABLED` | true | Enable per-username lockout |
| `LOGIN_LOCKOUT_MAX_ATTEMPTS` | 5 | Failures within the window before locking |
| `LOGIN_LOCKOUT_WINDOW_SECONDS` | 900 | Rolling failure window |
| `LOGIN_LOCKOUT_DURATION_SECONDS` | 900 | Lock duration once tripped |

- Keyed on the normalized username, Redis-backed (`services/login_lockout.py`), **fails OPEN** on a Redis outage (a blip must not lock out the household; the per-IP limit remains the backstop).
- A locked login returns the **same opaque 401** as bad credentials (no username-enumeration oracle). The event is surfaced via logging + the `login_failure_total{reason="locked_out"}` metric.
- Trade-off: an attacker who knows a username can lock that user out for at most the duration (bounded, env-disable-able). This is the standard lockout trade-off, accepted over unbounded credential-stuffing.

### WebSocket

WebSocket rate limiting uses a sliding window algorithm (`WS_RATE_LIMIT_ENABLED=true`):

| Limit | Default | Notes |
|-------|---------|-------|
| Per second | 50 | Accommodates audio streaming (~12.5 chunks/s) |
| Per minute | 1000 | Allows longer recordings |
| Max connections per IP | 10 | `WS_MAX_CONNECTIONS_PER_IP` |
| Max message size | 1 MB | `WS_MAX_MESSAGE_SIZE` |
| Max audio buffer | 10 MB | `WS_MAX_AUDIO_BUFFER_SIZE` |

## Trusted Proxies

When behind a reverse proxy (nginx, Traefik), configure `TRUSTED_PROXIES` so rate limiting uses the real client IP instead of the proxy IP:

```bash
TRUSTED_PROXIES=172.18.0.0/16,127.0.0.1
```

- **When `TRUSTED_PROXIES` is configured** (spoof-resistant, #693): reads `X-Forwarded-For` / `X-Real-IP` only when the direct peer is a trusted proxy, and resolves the client by walking the `X-Forwarded-For` chain **right-to-left, returning the right-most address that is NOT a trusted proxy** (the genuine client from our trust boundary). An attacker cannot inject an untrusted address to the right of our own proxy hop, so `X-Forwarded-For` cannot be spoofed to change rate-limit identity.
- **When `TRUSTED_PROXIES` is empty (default):** legacy backwards-compatible behavior — all proxies are trusted and `X-Forwarded-For[0]` (the left-most entry) is used. This preserves per-client keying behind a proxy out of the box, but **is spoofable** (a client can forge `X-Forwarded-For[0]`). Set `TRUSTED_PROXIES` to the proxy's network to get the spoof-resistant walk above. (Flipping the empty default to "use the direct socket IP" would collapse every client into the proxy's single IP bucket — a cluster-wide rate-limit DoS — so the default stays legacy; hardening is opt-in via `TRUSTED_PROXIES` at the auth-on cutover.)

## Auth Observability

Failed logins and authorization denials emit structured logs and Prometheus counters (`METRICS_ENABLED=true`) so credential-stuffing and privilege-probing are visible to monitoring. Responses stay opaque; only the telemetry distinguishes cases.

| Metric | Labels | Fires on |
|--------|--------|----------|
| `renfield_login_failure_total` | `reason` (`bad_credentials`, `inactive`, `locked_out`) | Each failed `/auth/login` |
| `renfield_authz_denied_total` | `permission` (required perm, or `inactive_account` / `password_change_required`) | Each 403 from `require_permission` / `require_any_permission` / the disabled-account + forced-rotation gates |

Labels are low-cardinality strings — never the username or token.

## Forced Password Rotation

A user with `must_change_password=true` (e.g. a bootstrapped admin with an auto-generated password) is enforced server-side in `get_current_user` (#694): every authenticated route returns `403 password_change_required` **except** an allowlist (`/api/auth/change-password`, `/api/auth/me`, `/api/auth/status`, `/api/auth/logout`), until the password is rotated via `/api/auth/change-password` (which clears the flag). Enforcement uses DB truth, so a token minted before the flag was set is still blocked. The `/auth/login` response carries `must_change_password` so the client can redirect straight to the change form.

## Login & User-Management Audit Hardening (2026-07-21)

A dedicated adversarial audit of the login/user-management surface (the first auth-on multi-user instance) landed these fixes. Full write-up: TODOS.md → "🔐 Full security audit — login & user management".

- **Session revocation via `token_epoch`.** Access + refresh JWTs carry an `epoch` claim = the user's `users.token_epoch` (migration `pc20260722`). `get_current_user`, `/api/auth/refresh`, `authenticate_websocket`, and the internal `/auth/verify` all reject a token whose `epoch` is older than the DB value. **Password change** (`/change-password` — bumps epoch, re-issues the caller a fresh pair so only OTHER sessions die) and **admin password reset** (`/users/{id}/reset-password` — bumps epoch + sets `must_change_password`) therefore invalidate every outstanding token for that user. Default 0 + missing-claim→0 means no mass logout on deploy.
- **Logout revokes BOTH tokens.** `/api/auth/logout` blacklists the access-token jti AND the refresh-token jti (sent in the body; refresh revoked first), and returns **503** rather than a misleading 200 if the revocation store write fails (`token_blacklist.add` reports success). The WS handshake now also honors the blacklist.
- **Grant-only-what-you-hold** (`models/permissions.py::missing_grantable_permissions`). A `roles.manage`/`users.manage` holder can only grant a role/user permissions the caller themselves holds (`admin` is a superset; MCP wildcards + hierarchy honored), and cannot change their own role — closing self-escalation to Admin. Editing a **system role's** permission set additionally requires `admin`. Auth-off (`user=None`) skips these caller-authority checks.
- **Last-admin lockout guards.** Demoting / deactivating / deleting the final active admin, or stripping `admin` from the only admin-granting role, is refused (400). (Known: the guard is TOCTOU-racy under concurrent admin edits — P2 in TODOS.)
- **Login timing equalization.** `authenticate_user` runs one bcrypt round against a fixed dummy hash on the user-not-found branch, so response latency no longer distinguishes real from unknown usernames.
- **Short-lived WS-scoped token (M2).** `POST /api/ws/token` returns a ~90s `scope:"ws"` access JWT (`WS_JWT_EXPIRE_SECONDS`) that `get_current_user`/`/auth/verify` reject on the REST/voice surfaces, so a WS token harvested from a proxy log is useless there. The **chat + kiosk** browser sockets now fetch it (`utils/wsToken.ts::fetchWsToken`) instead of putting the full 24h JWT in `?token=`; the **voice** socket keeps the full JWT (separate voice-server auth) pending the voice-server auth redesign. The legacy `#access_token=` fragment handler is now behind the `VITE_SSO_LEGACY_FRAGMENT` kill switch and only accepts a valid unexpired access JWT (`utils/ssoFragment.ts`) — removal awaits the SSO `?code=` cutover (TODOS).
- **Auth-audit remediation (2026-08).** A read-only auth/login audit drove: a per-document ACL (`api/routes/knowledge.py::check_document_access`) that closes a KB-less/orphaned-KB **document IDOR** (owner resolved via `Document.atom_id → atoms.owner_user_id`) and a source-ownership check on `POST /documents/move`; **presence** read/analytics endpoints (`/rooms`, `/room/{id}`, `/user/{id}`, `/analytics/*`) gated on `ROOMS_READ`/`ROOMS_MANAGE` with a per-user IDOR guard (were unauthenticated); and an auth gate on the previously-open `/ws/wakeword`. The account-lockout-DoS finding was reviewed and **left as an accepted tradeoff** (username lockout can't prevent targeted-lockout-DoS without weakening brute-force protection — needs a dedicated 2FA/unlock design).

## Auth Config Posture

The deployment posture is set **explicitly** in the ConfigMap (`k8s/configmap.yaml`) rather than left to code defaults, so the running state is auditable (#697): `RENFIELD_ENV`, `AUTH_ENABLED`, `WS_AUTH_ENABLED`, `ALLOW_REGISTRATION`, `CORS_ORIGINS`, `TRUSTED_PROXIES`, `API_RATE_LIMIT_STORAGE_URI`. Current values are the deliberate single-user, auth-off LAN posture (byte-identical to the code defaults).

A startup validator (`assert_auth_config_consistency` in `utils/config.py`) **refuses to boot** on an incoherent combo:

- **Hard fail:** `AUTH_ENABLED=true` with `WS_AUTH_ENABLED=false` — the WebSocket chat surface would be unauthenticated and the WS session-ownership check (#657) silently disabled. The two flags must be enabled together.
- **Warn:** `AUTH_ENABLED=true` with wildcard `CORS_ORIGINS='*'`; a production `RENFIELD_ENV` with `ALLOW_REGISTRATION=true`.

`RENFIELD_ENV` is a tracked setting; a real-deployment value (production/prod/staging) also arms the insecure-`SECRET_KEY` boot guard (#692), so a strong key must be provisioned before the auth-on cutover. The ConfigMap carries a commented **AUTH-ON CUTOVER** checklist of the values to flip together.

## Circuit Breaker

The circuit breaker protects against cascading failures when the LLM or agent loop is unavailable.

**States:** `CLOSED` (normal) → `OPEN` (failing, reject fast) → `HALF_OPEN` (testing recovery)

| Setting | Default | Description |
|---------|---------|-------------|
| `CB_FAILURE_THRESHOLD` | 3 | Consecutive failures to open circuit |
| `CB_LLM_RECOVERY_TIMEOUT` | 30s | Wait before testing LLM recovery |
| `CB_AGENT_RECOVERY_TIMEOUT` | 60s | Wait before testing agent recovery |

Implementation: `src/backend/utils/circuit_breaker.py`

## Cross-Cluster LLM/Voice Ingress Allowlist

When GPU services in the `renfield` namespace are exposed to **other clusters** over Traefik `*.test.local` ingresses (e.g. acting as the LLM/voice tier for a Reva prod cluster — see [KUBERNETES_DEPLOYMENT.md](KUBERNETES_DEPLOYMENT.md#cross-cluster-service-exposure-llm--voice)), those endpoints must be IP-restricted. `ollama` / `llama-server` have **no built-in auth**, so an open `*.test.local` ingress lets any LAN host drive unauthenticated, uncapped inference on the GPU (DoS / cost-abuse).

**Control:** a Traefik `IPAllowList` middleware (`llm-ingress-allowlist`) on each external LLM/voice ingress, `sourceRange` = the consuming cluster's node subnet.

**Prerequisite:** `traefik-web-service` must use `externalTrafficPolicy: Local`. With MetalLB-L2 + the default `Cluster` policy, kube-proxy SNATs the client to a node IP before Traefik sees it, defeating any source-IP allowlist. Because Calico `natOutgoing` SNATs the consumer's pod IP to its node IP, allowlist the **node** subnet (not pod CIDRs — clusters often share the `172.16/16` pod range).

> In-cluster consumers use the ClusterIP Services directly and are unaffected by this gate.

## Secrets Management

Production uses Docker Compose file-based secrets (`/run/secrets/`) instead of `.env` for sensitive values. See [SECRETS_MANAGEMENT.md](SECRETS_MANAGEMENT.md) for details.

## Security Best Practices

1. **Secrets:** Use Docker secrets in production, never `.env` for sensitive values. See [SECRETS_MANAGEMENT.md](SECRETS_MANAGEMENT.md).
2. **HTTPS:** Use HTTPS in production (configured in nginx).
3. **Rate Limiting:** API and WebSocket rate limiting is enabled by default.
4. **Input Validation:** All API inputs are validated with Pydantic.
5. **SQL Injection:** SQLAlchemy ORM prevents SQL injection.
6. **XSS:** React's JSX escaping + CSP headers prevent XSS.
7. **CORS:** Configurable via `CORS_ORIGINS` (default `*` for development, restrict in production).

## Reporting Security Issues

Please report security vulnerabilities privately via GitHub Security Advisories.
