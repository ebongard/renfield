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

**Development (more permissive for HMR):**
```
default-src 'self';
script-src 'self' 'unsafe-inline' 'unsafe-eval';
style-src 'self' 'unsafe-inline';
img-src 'self' data: blob:;
font-src 'self' data:;
connect-src 'self' ws: wss: http://localhost:* ws://localhost:*;
media-src 'self' blob:;
worker-src 'self' blob:;
frame-ancestors 'none';
```

**Production:**
```
default-src 'self';
script-src 'self' 'unsafe-inline';
style-src 'self' 'unsafe-inline';
img-src 'self' data: blob:;
font-src 'self' data:;
connect-src 'self' ws: wss:;
media-src 'self' blob:;
worker-src 'self' blob:;
frame-ancestors 'none';
```

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

See [ACCESS_CONTROL.md](../ACCESS_CONTROL.md) for details on:
- JWT-based authentication
- Role-Permission Based Access Control (RPBAC)
- Voice authentication

## Security Best Practices

1. **Environment Variables:** Never commit secrets to git. Use `.env` files.
2. **HTTPS:** Use HTTPS in production (configured in nginx).
3. **Rate Limiting:** API rate limiting is enabled by default.
4. **Input Validation:** All API inputs are validated with Pydantic.
5. **SQL Injection:** SQLAlchemy ORM prevents SQL injection.
6. **XSS:** React's JSX escaping + CSP headers prevent XSS.

## Reporting Security Issues

Please report security vulnerabilities privately via GitHub Security Advisories.
