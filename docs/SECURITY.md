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

- Only reads `X-Forwarded-For` / `X-Real-IP` headers when the direct client IP is in a trusted network
- If `TRUSTED_PROXIES` is empty (default), all proxies are trusted (backwards-compatible)

## Circuit Breaker

The circuit breaker protects against cascading failures when the LLM or agent loop is unavailable.

**States:** `CLOSED` (normal) → `OPEN` (failing, reject fast) → `HALF_OPEN` (testing recovery)

| Setting | Default | Description |
|---------|---------|-------------|
| `CB_FAILURE_THRESHOLD` | 3 | Consecutive failures to open circuit |
| `CB_LLM_RECOVERY_TIMEOUT` | 30s | Wait before testing LLM recovery |
| `CB_AGENT_RECOVERY_TIMEOUT` | 60s | Wait before testing agent recovery |

Implementation: `src/backend/utils/circuit_breaker.py`

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
