# Browser voice on auth-on instances (xidra)

Status: **code + infra prepared, `FEATURE_VOICE` NOT flipped** (2026-09-15,
branch `feat/xidra-browser-voice`). The flag flip is the last rollout step and a
separate, explicit decision.

## What the path looks like

```
browser (https://x-ren.local)
  │ 1. GET /api/ws/token?purpose=voice      → ~90 s JWT, scope "voice" (xidra backend)
  │ 2. GET /api/config/features             → voice_client_id = "xidra"
  │ 3. wss://x-ren.local/ws/voice?token=…&client=xidra   (Origin: https://x-ren.local)
  ▼
Traefik websecure ── IngressRoute voice/voice-server-xidra-browser
  │   Host(x-ren.local) && PathPrefix(/ws/voice) && Header(Origin, https://x-ren.local)
  ▼
voice-server (ns voice, :8080, AUTH_MODE=registry)
  │ 4. registry row "xidra" → POST http://backend.renfield-xidra:8000/api/internal/auth/verify
  │    (+ X-Verify-Secret = row verify_secret)
  ▼
xidra backend: verify accepts any non-"ws" scope for an active user (epoch + blacklist checked)
  │ 5. final_transcript + speaker_embedding[192] → browser → chat WS (xidra backend)
  ▼
chat_handler._resolve_wire_speaker: voice_originated=True; resolver ONLY if
SPEAKER_RECOGNITION_ENABLED (xidra: false → no voiceprint is stored)
```

## Decisions

| # | Decision |
|---|---|
| D1 | Route = Traefik `IngressRoute` **in ns `voice`** (Traefik v3.3 here has no cross-namespace / ExternalName routing). Match `Host` + `PathPrefix(/ws/voice)` + exact `Origin` pin, `priority: 1000` (beats the frontend catch-all), **no** retry / buffering / compress middlewares. |
| D2 | `tls: {}` — Traefik serves `xidra-tls` by SNI from its store (verified 2026-09-15: SNI `x-ren.local` → CN `x-ren.local`, SAN `x-ren.local, *.x-ren.local`). Fallback only if a wrong cert appears: copy the secret into ns `voice`. |
| D3 | New setting `VOICE_BROWSER_CLIENT_ID` → `/api/config/features.voice_client_id`. Frontend precedence runtime → `VITE_VOICE_CLIENT_ID` → omit. Read at CONNECT time; the socket waits for the features. Distinct from `VOICE_CLIENT_ID` (backend→voice-server header). |
| D4 | Speaker recognition **off** on xidra (all four `SPEAKER_*` knobs) **plus** code gates, so no voiceprint is persisted on any path: `chat_handler` + the resolver (voice turns), admin enrollment routes/services (409 / refusal), and meetings (the per-cluster ECAPA `embedding` is stripped before `Meeting.segments` is ever written — independent of flags — and fingerprints, which persist a centroid, need `meeting_fingerprints_enabled` AND `speaker_recognition_enabled`). Read-only count 2026-09-15: 0 meetings on either instance → nothing to purge; `bin/purge_meeting_segment_embeddings.py` exists for legacy rows (dry-run first). Opt-in later, only after a GDPR assessment (TODOS). The household deviates deliberately (speaker recognition stays on with per-member consent — D-6 in `household-auth-on-cutover.md` §3). |
| D5 | Users may enable the browser wake word themselves once voice is on; it stays gated only on `isFeatureEnabled('voice')`. |
| D6 | No in-app notice — the browser's own microphone permission prompt is the consent surface. |
| D7 | Voice-server hardening (token redaction in the access log, per-client origins) → next voice-server release, not this change. |
| — | **No maximum recording length** anywhere on this path (client, Traefik, voice-server). |

The composer mic is now gated on `isFeatureEnabled('voice')` too (it wasn't —
only the wake-word controls were), so xidra shows no mic until the flag flips.

## Preconditions (verified read-only 2026-09-15)

| Check | Result |
|---|---|
| Registry rows (`voice/voice-registry-auth`, `AUTH_CLIENTS`) | `reva` (verify, secret), `renfield` (anonymous), `xidra` → `backend.renfield-xidra:8000/api/internal/auth/verify`, secret set |
| Voice pod → xidra verify, `{"token":"probe"}` | **401** (reachable, opaque rejection — netpol egress OK) |
| NetworkPolicies ns `voice` | Traefik (ns default) → :8080 allowed; egress → `renfield-xidra:8000` allowed |
| TLS by SNI | `xidra-tls` served for `x-ren.local` |
| `wss://x-ren.local/ws/voice` today | 404 (no route yet — expected) |
| verify_secret: registry row `xidra` vs xidra backend (Secret + running pod env) | Identical (three-way SHA256-prefix comparison, values never printed). The backend reads it from `renfield-secrets` key `internal-auth-verify-secret` (NOT `renfield-env-private`). |

## Rollout order (each step reversible; stop before the flag)

1. **Precondition — verify-secret three-way check** (must print three identical
   prefixes; any difference = every browser voice turn 401s). Values are never
   printed, only `shasum -a 256 | cut -c1-12`:
   ```bash
   # a) registry row xidra.verify_secret (ns voice)
   kubectl --context renfield-private -n voice get secret voice-registry-auth -o json \
     | jq -r '.data.AUTH_CLIENTS | @base64d | fromjson
              | (if type=="array" then (.[] | select(.id=="xidra")) else .xidra end) | .verify_secret' \
     | tr -d '\n' | shasum -a 256 | cut -c1-12
   # b) the backend's Secret (the source of the env var in k8s/backend.yaml)
   kubectl --context renfield-private -n renfield-xidra get secret renfield-secrets -o json \
     | jq -r '.data["internal-auth-verify-secret"] | @base64d' | tr -d '\n' | shasum -a 256 | cut -c1-12
   # c) what the RUNNING backend actually loaded
   kubectl --context renfield-private -n renfield-xidra exec deploy/backend -c backend -- \
     sh -c 'printf %s "$INTERNAL_AUTH_VERIFY_SECRET"' | shasum -a 256 | cut -c1-12
   ```
   The env entry must also be present in x-ren `k8s/backend.yaml` (it was live
   only via a patch until `config/xidra-browser-voice`); a replace/recreate from
   a manifest without it would leave the verify endpoint unauthenticated.
2. **Renfield release** containing this branch (backend + frontend image) on
   xidra. Harmless while `FEATURE_VOICE=false`: `voice_client_id` is served but
   unused, the mic is hidden, the speaker gate is inert without voice turns.
3. **xidra ConfigMap** (`x-ren`, `k8s/renfield-env.configmap.yaml` — the live one;
   `k8s/configmap.yaml` carries the same keys): `VOICE_BROWSER_CLIENT_ID=xidra`,
   `SPEAKER_RECOGNITION_ENABLED=false`, `SPEAKER_AUTO_ENROLL=false`,
   `SPEAKER_CONTINUOUS_LEARNING=false`, `SPEAKER_VOCAB_CAPTURE_ENABLED=false`;
   `FEATURE_VOICE` stays `false`. Backend rollout.
4. **IngressRoute** (`private_k8s/voice-server/41-xidra-browser-ingressroute.yaml`)
   + the updated netpol comment. Verify with the no-credential checks below.
5. **(Separate decision)** `FEATURE_VOICE=true` on xidra + backend rollout, then a
   browser E2E with a real user (mic prompt → transcript → answer → TTS).

Rollback: delete the IngressRoute (route gone → 404), revert the ConfigMap keys;
the code is inert without `FEATURE_VOICE`.

## No-credential E2E checks (after step 4, before the flag)

```bash
LB=192.168.1.230
# TLS: the right certificate by SNI
echo | openssl s_client -connect $LB:443 -servername x-ren.local 2>/dev/null \
  | openssl x509 -noout -subject -ext subjectAltName
# Route + Origin pin: wrong/missing Origin must NOT reach the voice-server (404 from the frontend catch-all)
curl -sk -o /dev/null -w 'no-origin=%{http_code}\n'    --resolve x-ren.local:443:$LB \
  -H 'Connection: Upgrade' -H 'Upgrade: websocket' -H 'Sec-WebSocket-Version: 13' \
  -H 'Sec-WebSocket-Key: dGhlIHNhbXBsZSBub25jZQ==' 'https://x-ren.local/ws/voice?client=xidra'
curl -sk -o /dev/null -w 'evil-origin=%{http_code}\n'  --resolve x-ren.local:443:$LB \
  -H 'Origin: https://evil.example' \
  -H 'Connection: Upgrade' -H 'Upgrade: websocket' -H 'Sec-WebSocket-Version: 13' \
  -H 'Sec-WebSocket-Key: dGhlIHNhbXBsZSBub25jZQ==' 'https://x-ren.local/ws/voice?client=xidra'
# Pinned Origin, bogus token: reaches the voice-server, which rejects the token
# (the voice-server's rejection — close/401 — NOT the frontend's 404/index.html)
curl -sk -i -m 5 --http1.1 --resolve x-ren.local:443:$LB \
  -H 'Origin: https://x-ren.local' \
  -H 'Connection: Upgrade' -H 'Upgrade: websocket' -H 'Sec-WebSocket-Version: 13' \
  -H 'Sec-WebSocket-Key: dGhlIHNhbXBsZSBub25jZQ==' \
  'https://x-ren.local/ws/voice?client=xidra&token=probe' | head -5
# Frontend/API unaffected by the priority-1000 route
curl -sk -o /dev/null -w 'app=%{http_code}\n' --resolve x-ren.local:443:$LB https://x-ren.local/
curl -sk -w '\n' --resolve x-ren.local:443:$LB https://x-ren.local/api/auth/status | jq '.features.voice'
# Verify endpoint reachable from the voice pod (opaque 401 expected)
kubectl --context renfield-private -n voice exec deploy/voice-server -- \
  curl -s -o /dev/null -w '%{http_code}\n' -X POST -H 'Content-Type: application/json' \
  -d '{"token":"probe"}' http://backend.renfield-xidra:8000/api/internal/auth/verify
```

After step 3 additionally: `curl … /api/config/features` needs a session, so
check it in the browser devtools (`voice_client_id: "xidra"`), and confirm the
composer shows **no** mic while `FEATURE_VOICE=false`.

## Follow-ups

See `TODOS.md` → "Browser voice on xidra — follow-ups": D4 speaker opt-in (GDPR
assessment first), D7 voice-server hardening. **F7 (household browser-voice route) is FIXED 2026-09-18** — the
orphaned `renfield/voice-server` Ingress from #1119 was removed and replaced by an
`IngressRoute` in ns `voice` targeting `voice-server-anon:8081` (the household is auth-off,
so the primary port cannot work); the household route is versioned as
`private_k8s/voice-server/42-household-browser-ingressroute.yaml`, sibling of this
runbook's `41-xidra-browser-ingressroute.yaml`.

## Background moved from CLAUDE.md (2026-09-20)

Details that lived in the `CLAUDE.md` cookie-session section and are not stated above. The
must-not-break summary is in `.claude/rules/auth.md`; the cookie session itself is described in
`docs/design/auth-cookie-session.md`.

**Voice WS token (#1128, live since `2026-08-25-voice-ws`).**

- With the HttpOnly-cookie session the browser holds no JS-readable JWT, so the browser voice WS
  fetches a short-lived `scope:"voice"` faucet token (`/api/ws/token?purpose=voice`) and sends it as
  `?token=` to the external voice-server.
- The voice-server's verify path (`/api/internal/auth/verify`) accepts any non-`ws` scope; REST and
  renfield's own `/ws/*` reject `scope:voice`.
- No voice-server change was needed.

**Client id on the wire.**

- The browser names its registry row via `?client=`, from the RUNTIME `voice_client_id` in
  `/api/config/features` (`VOICE_BROWSER_CLIENT_ID`, validated `^[a-z0-9_-]{0,64}$`) → build-time
  `VITE_VOICE_CLIENT_ID` → omitted (household byte-identical).
- `useVoiceStream` awaits the id at CONNECT time (`fetchVoiceClientId` → `ensureQueryData`), so the
  socket never opens before the features are known. The pure `buildVoiceWsUrl` is exported.

**Speaker-recognition privacy gate — what it closed and where it sits.**

- Before the gate, every browser voice turn stored an ECAPA voiceprint and an
  "Unbekannter Sprecher #N" row (Art. 9 GDPR) even with recognition off.
- Voice turns: `chat_handler._resolve_wire_speaker` resolves a wire speaker embedding ONLY when
  `speaker_recognition_enabled` (`voice_originated` still derives from the embedding), and
  `speaker_resolver.resolve_speaker_from_embedding` refuses before any DB access when it is off.
  This covers chat-WS, voice and satellite resolution.
- Admin enrollment: `POST /api/speakers/enroll`, `/candidates/promote`, `/{id}/enroll` → 409; the
  enrollment service refuses too.
- Meetings: the voice-server's per-cluster ECAPA `embedding` is NEVER stored on `Meeting.segments`
  regardless of flags (`meeting_pipeline.strip_biometric_fields` / `_set_segments`, the only segments
  write path; re-render and relabel clean legacy rows; `GET …/segments` filters them).
- Legacy rows: `bin/purge_meeting_segment_embeddings.py` (`--dry-run`/`--commit`, counts only).
