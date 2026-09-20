---
paths:
  - "k8s/**"
  - "bin/deploy-production.sh"
  - "bin/k8s-drift-check.sh"
  - "src/backend/Dockerfile"
  - "src/frontend/nginx.conf"
---
# Deploy & manifests

Loaded only when a manifest or the deploy script is read. The runbook is the `deploy-production` skill
(`.claude/skills/deploy-production/SKILL.md`) — user-invoked; canonical cluster reference `docs/KUBERNETES_DEPLOYMENT.md`.

## Reality
- **GitHub CI does not run for this project** (`ci.yml`, `pr-check.yml`, `release.yml` are kept for the audit trail only;
  a tag builds nothing). Backend tests run on the build box `192.168.1.159` in the `renfield-backend` container; images
  are built there and pushed to the registry; production is the private k8s cluster (context `renfield-private`).
- The registry hostname is NOT committed: manifests carry the placeholder `your-registry.example/renfield/…`, scripts
  need `RENFIELD_REGISTRY`.
- Two instances: household (`renfield`, auth off) and xidra (`renfield-xidra`, auth on). xidra's manifests and config
  live in the private `x-ren` repo — never apply household files there.

## Never
- Never `kubectl apply -f` a placeholder manifest raw (`ImagePullBackOff`, and a `:latest` apply moves a pinned deploy):
  render first with `bin/k8s-drift-check.sh --render <file> | kubectl -n <ns> apply -f -`. Run the drift check before
  ANY apply; it cannot see fields that exist only live.
- Never put a `namespace:` field into `k8s/alembic-upgrade-job.yaml` — it overrides `-n` and would silently migrate the
  wrong instance. Apply the Job BEFORE the rolling restart.
- Never rebuild the `renfield-mcp-config` ConfigMap from files: `kg_scopes.yaml` is not in the repo and the live
  `mail_accounts.yaml` is not the committed default. Swap ONLY the changed key. `mcp_servers.yaml`, `agent_roles.yaml`,
  `kg_scopes.yaml`, `mail_accounts.yaml` are ConfigMap-served and must NEVER appear in the backend image.
- Never `kubectl patch` the live `renfield-env` ConfigMap as the source of truth: edit `k8s/configmap.yaml`, commit,
  apply, then restart backend + workers. A live change that is not in git is a bug.
- Never touch satellites from a deploy (Pi Zero SD cards brick on a bad restart) — see `.claude/rules/satellites.md`.

## Ownership
Images are owned by `kubectl set image` (the deploy script); everything else in a workload is owned by the manifest.
`frontend` and `voice-server` run pinned tags, so `rollout restart` is a no-op for them. Every backend rebuild must
stage the private plugin packages named in `PLUGIN_MODULES` via `RENFIELD_BACKEND_EXTRA_PACKAGES`; the script refuses to
push an image that cannot import them. Roll all four backend-image deployments together (backend, document-worker,
meeting-worker, pdf-split-worker).

## Frontend propagation
The frontend is a PWA: `sw.js`, `registerSW.js`, `index.html`, `manifest.webmanifest` must be `no-cache`, hashed bundles
`immutable`. A location with its own `add_header` stops inheriting the security headers — re-declare them. A
header-only change needs a build bump (`__BUILD_STAMP__`) or existing clients keep the stale shell. Open tabs need a
reload after a frontend deploy.

## Probes
Backend readiness `/health/ready` (DB reachability), liveness `/health/live` (dependency-free — a DB-dependent liveness
would restart-storm every replica during a DB outage). A rollout against a dead DB waits at NotReady; that is intended.
