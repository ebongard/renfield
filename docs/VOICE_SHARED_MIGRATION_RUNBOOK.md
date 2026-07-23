# Runbook: migrate the renfield household onto the shared voice-server

**Status:** PENDING — do this in a maintenance window. Tracked as T31 in the
Reva voice-server extraction plan (`reva:docs/architecture/voice-server-extraction-plan.md`).
**Owner:** whoever runs the renfield-private cluster.
**Est. window:** ~30–45 min (Approach B) incl. one shared-instance restart.

---

## Why this exists

The voice-server was consolidated (2026-07) into ONE shared instance in the
`voice` namespace of the renfield-private cluster (node `k8s-gpu-3`, RTX
4060 Ti). Reva (REST + webchat WS) already runs on it. The household
(renfield, ns `renfield`) does **not** yet — it still points at its own
`voice-server` Deployment, which is **scaled to 0** (it lost the single GPU
to the shared instance during the Reva cutover — the plan's "keep the old
one as rollback" is impossible on a one-GPU node). So **household voice
currently runs on the in-process CPU fallback** (works, but slow: STT ~3–5 s
for 10 s audio vs <1 s on the GPU). This runbook moves it onto the shared
GPU instance.

## Current state (verified 2026-07-19)

| Thing | Value |
|---|---|
| Household backend image | `your-registry.example/renfield/backend:2026-07-17-mtg-delete` — **predates T8** (`cfe8a005`), so it CANNOT send the `X-Voice-Client` header |
| Household `VOICE_SERVER_URL` (cm `renfield-env`, ns renfield) | `http://voice-server:8080` — a Service with **no endpoints** (deployment scaled to 0) → every call fails → CPU fallback |
| Old renfield-ns `voice-server` Deployment | `replicas: 0` (scaled down in Phase 4). Its manifest still says `1`, so a plain redeploy would recreate a harmless Pending pod. |
| Shared instance | ns `voice`, image `voice-server@sha256:e93a2b46…` (v0.3.1), `AUTH_MODE=registry`. Registry: `reva` → verify_url (+ T11 secret), `renfield` → `{anonymous: true}`. |
| Anonymous listener | port **8081** (`ANON_PORT`), Service `voice-server-anon.voice:8081`. Honored ONLY on this port, fenced by NetworkPolicy `allow-anon-8081-renfield-only` (ns `renfield` sources only). |
| Registry auth rule | Every caller must send a client id (`X-Voice-Client` / `?client=`). The household's row is `anonymous: true` — no token needed, but a client id is still required by today's code. |

**The blocker:** the shared instance's registry auth requires `X-Voice-Client`,
but the household backend (old image) doesn't send it. Two ways to close that
gap — pick one.

---

## Approach B — `anon_default_client` (RECOMMENDED)

Add a small voice-server option: on the fenced anon port, a **missing**
`X-Voice-Client` defaults to a configured client id. Then the household backend
needs **no rebuild and no migrations** — just a ConfigMap repoint. Safe because
:8081 is already NetworkPolicy-fenced to ns `renfield`.

Cost: one shared-instance release + restart (~1–2 min; Reva voice blips to CPU
fallback during the reload — not an outage).

### B1. Code: add `anon_default_client` to voice-server

`voice-server/voice_server/config.py` — add to `Settings`:
```python
    # On the anon listener (anon_port), a request with NO X-Voice-Client
    # defaults to this client id. Lets a client that predates the
    # X-Voice-Client header (e.g. the household backend) use the shared
    # instance via the NetworkPolicy-fenced anon port. Empty = off.
    anon_default_client: str = ""   # env ANON_DEFAULT_CLIENT
```

`voice-server/voice_server/auth.py` — in `_validate_registry`, BEFORE the
`if not client_id: raise AuthError(...)` check:
```python
    if not client_id and via_anon_port and settings.anon_default_client:
        client_id = settings.anon_default_client
```
(The defaulted id must be an `anonymous: true` row — it then returns the
anonymous payload as usual. Add a unit test: anon port + no client-id +
`anon_default_client=renfield` → accepted as client `renfield`; primary port
same input → still rejected.)

Bump `voice_server/__init__.py` `__version__` to `0.3.2` and add a
`CHANGELOG.md` entry.

### B2. Release + deploy the shared instance

```bash
# On a host with SSH to the build box:
renfield/bin/release-voice-server.sh v0.3.2        # builds, pushes, records digest
# Pin the new digest in private_k8s/voice-server/10-deployment.yaml
# (image: ...voice-server@sha256:<from voice-server/RELEASES.md>)
# Add the env to the same deployment:
#   - name: ANON_DEFAULT_CLIENT
#     value: "renfield"
kubectl --context renfield-private apply -f private_k8s/voice-server/10-deployment.yaml
kubectl --context renfield-private -n voice rollout status deploy/voice-server
# (~1–2 min GPU reload; Reva voice on CPU-fallback meanwhile)
```

### B3. Repoint the household backend

```bash
# ns renfield — repoint at the anon Service (in-cluster, port 8081):
kubectl --context renfield-private -n renfield patch cm renfield-env --type=merge \
  -p '{"data":{"VOICE_SERVER_URL":"http://voice-server-anon.voice:8081"}}'
kubectl --context renfield-private -n renfield rollout restart deploy/backend
kubectl --context renfield-private -n renfield rollout status  deploy/backend
```
No `VOICE_CLIENT_ID` needed on the household backend (the anon default supplies
it). The `allow-anon-8081-renfield-only` NetworkPolicy already permits ns
`renfield` → :8081.

### B4. Repoint the household meeting-worker

`k8s/meeting-worker.yaml` hardcodes an init wait `nc -z voice-server.renfield 8080`
and reads `VOICE_SERVER_URL`. Update BOTH to the anon Service:
```
nc -z voice-server-anon.voice 8081
```
and ensure its `VOICE_SERVER_URL` resolves to `http://voice-server-anon.voice:8081`
(it reads the same `renfield-env`, so B3 covers it — just fix the `nc` wait).
Re-apply + restart `deploy/meeting-worker`. (`/transcribe-meeting` works on the
anon port; the worker's service-token is ignored by the anonymous row.)

Repeat the `nc` fix for the **renfield-xidra** meeting-worker when x-ren
migrates (Phase 3.3), not here.

---

## Approach A — rebuild the household backend with T8 (alternative)

Cleaner auth model (household sends a real `X-Voice-Client: renfield`), no
voice-server change — but it's a household backend build + deploy.

- **Do NOT** bump the household submodule/image straight to renfield `main`:
  `main` is ahead of the deployed `2026-07-17-mtg-delete` by the
  `pc20260718_meeting_minutes` migrations — a plain bump runs them. Either
  do a full, planned schema-evolution bump (read the schema-evolution notes
  first), OR cherry-pick just T8 (`cfe8a005`, touches `utils/config.py` +
  `services/voice_server_client.py`) onto the deployed commit and build that
  (migration-free — mirrors how Reva did it).
- Then: `VOICE_CLIENT_ID=renfield` + `VOICE_SERVER_URL=http://voice-server-anon.voice:8081`
  in `renfield-env`, restart backend + meeting-worker (with the B4 `nc` fix).

Use A only if you specifically want household off the anonymous-default path
(e.g. you plan to give household real per-user voice identity later).

---

## Verification (either approach)

```bash
# 1. Shared pod healthy, correct version
kubectl --context renfield-private -n voice exec deploy/voice-server -- \
  curl -sf localhost:8080/health      # {"status":"ok","version":"0.3.2",...}

# 2. From a household backend pod, the anon path is reachable + authenticates:
POD=$(kubectl --context renfield-private -n renfield get pod -l app.kubernetes.io/name=backend \
      -o jsonpath='{.items[0].metadata.name}')
kubectl --context renfield-private -n renfield exec "$POD" -- \
  curl -s -o /dev/null -w '%{http_code}\n' -X POST -F 'audio=@/dev/null' \
  http://voice-server-anon.voice:8081/api/voice/stt
#   → 400 (passed auth via the anon default, failed only on empty audio).
#     401 = auth still rejecting (anon_default_client not set, or wrong port).

# 3. Real household voice turn (satellite or webchat) transcribes on GPU
#    (<1 s for 10 s audio, not 3–5 s). Watch the shared pod's access log:
kubectl --context renfield-private -n voice logs deploy/voice-server -f | grep -E '/api/voice|/ws/voice'

# 4. Reva unaffected (it uses the reva registry row, not the anon path):
#    curl reva /api/health → voice.ok: true.
```

## Rollback

- Household: `kubectl -n renfield patch cm renfield-env --type=merge -p
  '{"data":{"VOICE_SERVER_URL":"http://voice-server:8080"}}'` + restart backend.
  (Returns to CPU fallback — the old renfield-ns pod is scaled to 0, so this is
  the pre-migration state, not a GPU path.)
- Approach B code: the `anon_default_client` default is `""` (off), so reverting
  the `ANON_DEFAULT_CLIENT` env (or rolling back to the prior image digest)
  disables it without touching Reva.

## Final cleanup (after the migration sticks)

Once household is stable on the shared instance, **remove** the old renfield-ns
`voice-server` Deployment + its Service/PVC from the renfield k8s manifests (it's
already scaled to 0; deleting the manifest stops a redeploy from recreating a
Pending pod). The shared instance is the only voice-server going forward.

---

## Reference

| Name | Value |
|---|---|
| Shared primary Service (authenticated clients) | `voice-server.voice:8080` |
| Shared anon Service (household) | `voice-server-anon.voice:8081` |
| Anon NetworkPolicy | `allow-anon-8081-renfield-only` (ns voice) — ns `renfield` → :8081 only |
| Household cm / key | `renfield-env` (ns renfield) / `VOICE_SERVER_URL` |
| Release script | `renfield/bin/release-voice-server.sh` |
| Digest ledger | `renfield/voice-server/RELEASES.md` |
| Shared-instance manifests | `private_k8s/voice-server/` |
| Extraction plan (full history + decisions) | `reva:docs/architecture/voice-server-extraction-plan.md` |
