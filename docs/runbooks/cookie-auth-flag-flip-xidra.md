# Runbook — Enable the HttpOnly-cookie session on xidra (`AUTH_COOKIE_ENABLED`)

**Change:** flip `AUTH_COOKIE_ENABLED` from off → **on** for the xidra business
instance (ns `renfield-xidra`), activating the HttpOnly-cookie + CSRF session
(PR #1125 / #1116). This is the deferred **Phase 5** of the cookie migration; the
code is already merged and deployed flag-off (byte-identical) on both instances.

**Scope:** xidra ONLY. The household (`renfield`) is auth-off and stays cookie-off.
**No image build, no DB migration.** Just a ConfigMap key + a rolling restart.
**Fully reversible** in ~1 minute (set the flag back to false + rollout).

**What flipping this changes:** login/refresh/change-password/logout now also set
HttpOnly `renfield_access` (Path=/) + `renfield_refresh` (Path=/api/auth/refresh)
cookies + a JS-readable `renfield_csrf` cookie; the backend reads the cookie first
(Bearer still accepted); CSRF is enforced on cookie-authed mutating requests; the
browser WS handshake authenticates via the cookie. The SPA stops persisting the
**30-day refresh token** to `localStorage` (the XSS win). The 24h access token is
still kept in `localStorage` for the voice WS + Bearer dual-read (full elimination
awaits the deferred voice-WS migration).

---

## 0. Pre-flight — verify the validator prerequisites (all already satisfied on xidra)

The backend REFUSES to boot with `AUTH_COOKIE_ENABLED=true` if any of these are
wrong (`config.py::assert_auth_config_consistency`). Confirm before flipping:

```bash
KX="kubectl --context renfield-private -n renfield-xidra"
$KX get cm renfield-env -o jsonpath='{.data.CORS_ORIGINS}{"\n"}'      # MUST NOT be "*"  → expect https://x-ren.local
$KX get cm renfield-env -o jsonpath='{.data.AUTH_ENABLED}{"\n"}'       # MUST be "true"
$KX get cm renfield-env -o jsonpath='{.data.WS_AUTH_ENABLED}{"\n"}'    # MUST be "true"
$KX get cm renfield-env -o jsonpath='{.data.RENFIELD_ENV}{"\n"}'       # "production" → COOKIE_SECURE must stay true
$KX get cm renfield-env -o jsonpath='{.data.COOKIE_SECURE}{"\n"}'      # empty(=default true) or "true" — NEVER "false" on prod
$KX get cm renfield-env -o jsonpath='{.data.AUTH_COOKIE_ENABLED}{"\n"}' # empty (currently off) — this is what we flip
```

Verified 2026-08-25: `CORS_ORIGINS=https://x-ren.local`, `AUTH_ENABLED=true`,
`WS_AUTH_ENABLED=true`, `RENFIELD_ENV=production`, `COOKIE_SECURE` unset (default
true). **No CORS pinning work is needed** — the origin is already pinned, which is
also what makes the WS CSWSH Origin allowlist real under cookie auth.

Also sanity-check the served hostname is HTTPS (Secure cookies require it): the
ingress serves `https://x-ren.local` — confirmed. If the browser reaches xidra
over plain HTTP the Secure cookie is dropped and login silently fails.

---

## 1. Record the change in git (source-of-truth audit trail)

Add the key to the committed xidra ConfigMap template so git stays authoritative.
Insert into the `data` block (alphabetical, just before `"AUTH_ENABLED"`):

`k8s/xidra/renfield-env.configmap.yaml`
```json
    "AUTH_COOKIE_ENABLED": "true",
    "AUTH_ENABLED": "true",
```

Commit on a branch → PR → merge (normal git-workflow; this is a one-line ops
change, no code review gate needed but keep the audit trail).

> ⚠️ **Do NOT `kubectl apply -f k8s/xidra/renfield-env.configmap.yaml`.** That
> committed file is a **template** — its `DATABASE_URL` carries the
> `__PG_PASSWORD__` placeholder, and a raw apply would overwrite the live
> ConfigMap's real DATABASE_URL with the placeholder. Apply the change with the
> surgical merge-patch in step 2 instead (the documented xidra pattern — same as
> the login-audit #2 ConfigMap change).

---

## 2. Apply the flag (surgical merge-patch — does NOT touch other keys)

```bash
KX="kubectl --context renfield-private -n renfield-xidra"
$KX patch cm renfield-env --type merge -p '{"data":{"AUTH_COOKIE_ENABLED":"true"}}'
$KX get cm renfield-env -o jsonpath='{.data.AUTH_COOKIE_ENABLED}{"\n"}'   # → true
```

Then roll the pods that read the ConfigMap so they pick up the new value:

```bash
$KX rollout restart deploy/backend deploy/document-worker
# meeting-worker too, if present on xidra:
$KX get deploy meeting-worker >/dev/null 2>&1 && $KX rollout restart deploy/meeting-worker
$KX rollout status deploy/backend --timeout=600s
$KX rollout status deploy/document-worker --timeout=600s
```

**If the backend CrashLoops here**, the validator rejected the posture — read the
reason and revert (step 5):
```bash
$KX logs deploy/backend -c backend --tail=40 | grep -iE "Inconsistent auth config|COOKIE"
```
(The only expected trigger would be a CORS/secure regression; pre-flight rules it out.)

No frontend rebuild is needed — the frontend already learns cookie-mode from
`/api/auth/status` (`auth_cookie_enabled`) and adapts at runtime.

---

## 3. Verify — the mandatory browser E2E (auth-on, real login)

Unregister the service worker / hard-reload first (PWA cache). Then, logged in as a
real xidra user, confirm end-to-end:

1. **Login sets the cookies.** On `POST /api/auth/login`, response carries three
   `Set-Cookie`s: `renfield_access` (HttpOnly, Secure, SameSite=Lax, Path=/),
   `renfield_refresh` (HttpOnly, Secure, SameSite=Lax, **Path=/api/auth/refresh**),
   `renfield_csrf` (Secure, SameSite=Lax, **NOT HttpOnly**). Check DevTools →
   Application → Cookies.
2. **`auth_cookie_enabled: true`** in `GET /api/auth/status`.
3. **Session works via the cookie:** `GET /api/auth/me` succeeds; hard-reload keeps
   you logged in (the session no longer depends on a JS-readable token).
4. **Refresh token is OUT of localStorage:** DevTools → Application → Local Storage
   → `renfield_refresh_token` is **absent** (the access token `renfield_access_token`
   may still be present — expected, deferred voice-WS migration).
5. **CSRF is enforced:** a mutating request (e.g. send a chat message / change a
   setting) succeeds (the SPA auto-attaches `X-CSRF-Token`). Optional negative
   check: a `curl` POST with the cookie but no `X-CSRF-Token` → **403**.
6. **WebSocket via cookie:** open chat → WS connects and streams a reply. Confirm
   the WS URL no longer needs the long-lived JWT (chat uses the short-lived faucet;
   the cookie is auto-sent on the handshake).
7. **Refresh-after-expiry:** wait past the access-cookie lifetime (or delete the
   `renfield_access` cookie in DevTools) → the next call transparently refreshes via
   the HttpOnly refresh cookie (no logout, no 429 storm on the login page).
8. **Change-password keeps THIS session, cuts others:** change your password → this
   device stays logged in (fresh cookies), other sessions are epoch-revoked.
9. **Logout clears cookies:** after logout, all three cookies are gone and `/me` → 401.
10. **No JWT in the URL/logs:** Traefik access logs show no `?token=<jwt>` on WS.
11. **Voice still works** (regression check — it reads the localStorage access
    token): a voice turn connects to the voice-server and responds.
12. **Reva (if OIDC ever enabled):** dark on xidra today; the `#access_token=`
    fragment path is untouched. No action.

Household regression check (should be untouched): `renfield` still cookie-off,
chat works, no `Set-Cookie`.

---

## 4. Keep git in sync

Confirm the merged git change (step 1) matches the live ConfigMap so a future
full reconcile doesn't drift:
```bash
diff <(kubectl --context renfield-private -n renfield-xidra get cm renfield-env -o jsonpath='{.data.AUTH_COOKIE_ENABLED}') <(echo -n true)
```

---

## 5. Rollback (instant, byte-identical revert)

If anything misbehaves — set the flag back off and roll:
```bash
KX="kubectl --context renfield-private -n renfield-xidra"
$KX patch cm renfield-env --type merge -p '{"data":{"AUTH_COOKIE_ENABLED":"false"}}'
$KX rollout restart deploy/backend deploy/document-worker
$KX rollout status deploy/backend --timeout=600s
```
Flag off → the reader falls back to Bearer, no cookies set, CSRF no-ops → exactly
the pre-flip behavior. The frontend still holds the localStorage tokens (it kept
writing the access token; on the next login it resumes writing the refresh token
too), so users stay logged in across the revert. Also revert the git one-liner.

---

## Notes / residual risk

- **What this delivers:** the 30-day refresh token leaves `localStorage` → an XSS
  can no longer steal the long-lived credential. CSRF protects the new cookie-auth
  surface (double-submit + SameSite=Lax).
- **What it does NOT yet deliver (deferred):** the 24h **access token still lives
  in `localStorage`** because the voice WS authenticates to the external
  voice-server (`internal_auth.verify`, needs a full access JWT, can't use the
  cookie). Full XSS elimination = the voice-WS migration follow-up. Don't claim
  "fully XSS-safe" until then.
- **SSO cutover / fragment handler removal** stays deferred (needs Reva `?code=`).
- **`SECRET_KEY` is untouched** (tri-purpose: JWT + Fernet-at-rest + BLE IRK —
  never rotate/split as part of this).
- **Household stays cookie-off** (auth-off; cookies are meaningless there and the
  validator would hard-fail `AUTH_COOKIE_ENABLED=true` + `AUTH_ENABLED=false`).

Design + full context: `docs/design/sso-token-handoff-hardening.md` (§9 cookie
alternative), CLAUDE.md §"JWT HttpOnly-cookie session + CSRF", `docs/ENVIRONMENT_VARIABLES.md`.
