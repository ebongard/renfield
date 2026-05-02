---
name: deploy-production
description: Production deployment guide for Renfield. Build box .159, Harbor registry, private k8s cluster. Triggers on "deploy", "Deployment", "production", "Produktion", "satellite deploy", "kubectl apply", "rsync", "registry.treehouse".
disable-model-invocation: true
---

# Production Deployment

## CRITICAL SAFETY

- **Pi Zero 2 W SD cards are EXTREMELY fragile** — SIGKILL during restart can brick the device. Always ask before restarting satellite services. See `references/satellite-deploy.md`.
- **Never force-push to main** and never bypass branch protection; main merges go through a PR (see `git-workflow` skill).
- **CI is intentionally non-functional for this project** — run tests on the .159 build box, not in CI.
- **ConfigMap-provided files are NOT in the image.** `mcp_servers.yaml`, `agent_roles.yaml`, `kg_scopes.yaml`, `mail_accounts.yaml` live only in the `renfield-mcp-config` ConfigMap. If the build-box working copy ever grows these as untracked files or directories at `src/backend/config/*`, the resulting image breaks the pod's subPath mount with `not a directory`. Keep `src/backend/.dockerignore` carving these paths out, and clean them on .159 if you see root-owned leftovers from kubelet bind-mount artefacts.

## Topology at a glance

| Role | Where | Notes |
|---|---|---|
| Build box | `192.168.1.159` (`renfield.local` mDNS often flaky — use the IP) | Docker Compose up for dev/test only. Used for `docker build` + `docker push`. Runs the full backend/test stack so pytest is executed here. **Not production.** |
| Container registry | `https://registry.treehouse.x-idra.de` | Harbor. Namespace paths used: `renfield/backend`, `renfield/frontend`. `harbor-pull-secret` already exists in the `renfield` k8s namespace. |
| Production | Private k8s cluster (kubectl context **`renfield-private`**) | GPU-accelerated LLM, Traefik ingress, Longhorn storage, MetalLB LB at `192.168.1.230`. Canonical doc: `docs/KUBERNETES_DEPLOYMENT.md`. |

Single-VM `renfield.local` deployment on `/opt/renfield` is legacy / build-box only. Anything calling itself a "prod rsync" is referring to the build box, not production.

## Release tag (audit trail)

```bash
# From the laptop, after the release commits are merged to main:
git checkout main && git pull
git tag -a vX.Y.Z -m "Release vX.Y.Z\n\n<release notes>"
git push origin vX.Y.Z
gh release create vX.Y.Z --title "vX.Y.Z — <one-line summary>" --notes "<body>"
```

`make release` exists but is interactive and only does the tag step. Manual `git tag` + `gh release create` lets you script the whole sequence.

The `release.yml` workflow on tag-push **does not actually build images** (CI is non-functional for this project). Tag is for the audit trail and the git history; the real build happens on .159.

## Build + push workflow (rsync-to-staging — preferred)

Images live in Harbor and are pulled by the cluster via `imagePullPolicy: Always` on the `:latest` tag (or pinned tags like `:vX.Y.Z`).

Both Dockerfiles expect **their own directory as the build context** — not the repo root. `COPY requirements.txt constraints.txt ./` and `COPY wakeword-models /app/wakeword-models` resolve against context root, and those files live at `src/backend/*`, not at the repo root. The wakeword models live at `data/wakeword-models/` in the repo and must be rsynced INTO the backend build context as `wakeword-models/`.

> **Why staging-dir, not `/opt/renfield`?** The `/opt/renfield` checkout on .159 is often on a feature/WIP branch with uncommitted changes. Building from there bakes the WIP into the image, OR (worse) clobbers the WIP if you do `git checkout`. The rsync-to-staging flow below isolates the build from the checkout.

### Step 1 — From the laptop, rsync the build contexts

```bash
# Prep a fresh staging dir on .159 (use the version tag in the path so concurrent
# releases don't trample each other and cleanup is unambiguous)
ssh evdb@192.168.1.159 "rm -rf /tmp/renfield-build-vX.Y.Z; mkdir -p /tmp/renfield-build-vX.Y.Z/src/backend /tmp/renfield-build-vX.Y.Z/src/frontend"

# Rsync src/backend (build context for the backend image)
rsync -avz --delete \
  --exclude='__pycache__' --exclude='.pytest_cache' --exclude='*.pyc' \
  --exclude='.coverage' --exclude='htmlcov' \
  --exclude='.env' --exclude='.env.local' --exclude='secrets/' \
  --exclude='Users/' \
  src/backend/ evdb@192.168.1.159:/tmp/renfield-build-vX.Y.Z/src/backend/

# Rsync src/frontend (build context for the frontend image)
rsync -avz --delete \
  --exclude='node_modules' --exclude='dist' --exclude='.vite' \
  --exclude='.cache' --exclude='.env' --exclude='.env.local' \
  src/frontend/ evdb@192.168.1.159:/tmp/renfield-build-vX.Y.Z/src/frontend/

# Rsync wakeword models INTO the backend build context (Dockerfile COPYs ./wakeword-models)
rsync -avz \
  data/wakeword-models/ evdb@192.168.1.159:/tmp/renfield-build-vX.Y.Z/src/backend/wakeword-models/
```

### Step 2 — On .159, build + push

```bash
ssh evdb@192.168.1.159

# Login to Harbor (skip if your session is already authenticated)
docker login registry.treehouse.x-idra.de

# Backend (CPU image, ~3.5 GB — torch pinned to +cpu wheels via constraints.txt)
# Slowest step; budget 10-20 min if requirements.txt changed (deps layer cache miss).
# 2-4 min if only Python source changed (deps layer cached).
cd /tmp/renfield-build-vX.Y.Z/src/backend
docker build \
  -t registry.treehouse.x-idra.de/renfield/backend:latest \
  -t registry.treehouse.x-idra.de/renfield/backend:vX.Y.Z \
  -f Dockerfile .
docker push registry.treehouse.x-idra.de/renfield/backend:latest
docker push registry.treehouse.x-idra.de/renfield/backend:vX.Y.Z

# Frontend (Nginx serving React build, ~144 MB; ~2-3 min build + push)
cd /tmp/renfield-build-vX.Y.Z/src/frontend
docker build \
  -t registry.treehouse.x-idra.de/renfield/frontend:latest \
  -t registry.treehouse.x-idra.de/renfield/frontend:vX.Y.Z \
  -f Dockerfile .
docker push registry.treehouse.x-idra.de/renfield/frontend:latest
docker push registry.treehouse.x-idra.de/renfield/frontend:vX.Y.Z
```

### Step 3 — Cleanup

```bash
# Remove the staging dir once the cluster has rolled (or even before — the images are in Harbor)
ssh evdb@192.168.1.159 "rm -rf /tmp/renfield-build-vX.Y.Z"
```

Always build + push a pinned tag (`:vX.Y.Z`) alongside `:latest` — gives you an immutable rollback target (`kubectl set image deploy/backend backend=.../backend:vX.Y.Z`).

**Why the image stays ~3.5 GB:** `src/backend/constraints.txt` pins `torch`/`torchaudio`/`torchvision` to the `+cpu` wheels so transitive deps (docling, easyocr, transformers) can't drag in the 2.7 GB CUDA runtime + 641 MB triton. Don't lift that constraint unless you've thought through the Harbor push timeout.

### Harbor 504 / "Client Closed Request" on the 2.66 GB pip-install layer

When `requirements.txt` changes (so the deps layer cache misses), Docker tries to upload a single 2.66 GB layer to Harbor. The ingress proxy in front of Harbor has been observed timing out on this with `received unexpected HTTP status: 504 Gateway Timeout` or `unknown: Client Closed Request`. The error reproduces on the same layer ID across multiple retries (verified during the v2.3.0 deploy 2026-05-01 — 4 attempts, same `ed85...` layer, same error).

Mitigations to try, in order:

1. **Wait + retry.** Harbor's proxy might be load-shedding. A few minutes can clear it.
2. **Push the layer first, then the manifest.** `docker push --quiet` cuts logging overhead. If Docker is wasting time on output buffering during the layer upload, the timeout window shrinks.
3. **Split the requirements install** — and stage the heavy packages OUTSIDE `/opt/venv` so the split survives the multi-stage `COPY`. Splitting the pip install into multiple RUN steps in the builder stage is **not enough** by itself: the runtime stage's `COPY --from=builder /opt/venv /opt/venv` collapses every site-packages file from every prior RUN into one giant layer at push time. The fix (landed in PR #512, v2.3.0) is to (a) split pip install into 5 RUN steps, AND (b) `mv` the heavy packages (torch, transformers, easyocr, docling*, speechbrain, cv2, ctranslate2, librosa) into `/opt/staging/{torch,ml,audio}/` after the installs, then `COPY --from=builder /opt/staging/torch/. /opt/venv/lib/python3.11/site-packages/` (one COPY per staging dir) before the catch-all `COPY --from=builder /opt/venv /opt/venv`. Result: 722 MB / 205 MB / 63 MB / 1.66 GB instead of one 2.65 GB layer — each pushed independently.
4. **Investigate the Harbor proxy config.** The ingress in front of `registry.treehouse.x-idra.de` likely has `client_max_body_size` and read/write timeouts set conservatively. Bumping `proxy_read_timeout`, `proxy_send_timeout`, `client_body_timeout`, and `proxy_request_buffering off` on the Harbor proxy fixes this for all Renfield builds. (Requires admin access to the Harbor host.)

When you hit this in the future: don't keep retrying blindly past 3 attempts — the layer ID failing is fixed, so the proxy/Harbor side is the issue. Stop the push, document which release tag couldn't ship, and surface to the operator.

## Cluster rollout

```bash
kubectl config use-context renfield-private

# Rolling restart to pull the new :latest. ALL FOUR deploys must be
# rolled (dlna-mcp also runs the backend image).
kubectl -n renfield rollout restart deploy/backend deploy/dlna-mcp deploy/document-worker deploy/frontend

# Or pin an explicit tag (force-pulls even if :latest is cached on the node)
kubectl -n renfield set image deploy/backend \
  backend=registry.treehouse.x-idra.de/renfield/backend:vX.Y.Z

# Wait for each rollout
kubectl -n renfield rollout status deploy/backend --timeout=600s
kubectl -n renfield rollout status deploy/dlna-mcp --timeout=600s
kubectl -n renfield rollout status deploy/document-worker --timeout=600s
kubectl -n renfield rollout status deploy/frontend --timeout=600s
```

> **Image-pull timing.** First pull of a 3.5 GB backend image takes 2-5 minutes per node.
> Subsequent rollouts on the same node hit the local cache and start in seconds.
> The pod sits in `PodInitializing` while the image transfers — that's not a stall.

### Verify all pods picked up the new image

```bash
# Image digests should match what `docker push` returned for :vX.Y.Z
kubectl -n renfield get pods -o jsonpath='{range .items[*]}{.metadata.name}{"\t"}{.status.containerStatuses[*].imageID}{"\n"}{end}' | grep -E "backend|frontend"
```

If a pod shows a stale digest, it's a `imagePullPolicy` issue. Force the pull with the explicit tag:

```bash
kubectl -n renfield set image deploy/<name> <container>=registry.treehouse.x-idra.de/renfield/backend:vX.Y.Z
kubectl -n renfield rollout status deploy/<name>
```

### Smoke test

```bash
kubectl -n renfield exec deploy/backend -c backend -- curl -sS http://localhost:8000/health
# Expect: {"status":"ok"}
```

### Migrations

**Before authoring a new migration**, query the live DB for the current single head. File naming and visual chain inspection don't catch silent collisions — Renfield's `versions/` directory has 50+ files with overlapping naming schemes:

```bash
kubectl -n renfield exec deploy/backend -c backend -- alembic heads
kubectl -n renfield exec deploy/backend -c backend -- alembic current
```

Both should return the same single revision in a healthy chain. Use that string verbatim as `down_revision` in the new migration file. If `heads` returns multiple revisions the chain is already forked — stop and fix it before adding more. (Verified painful 2026-05-02: a chain collision blocked the v2.4.2 deploy with `Multiple head revisions are present` until a fix-forward PR re-pointed the migration.)

**Applying migrations during deploy:**

```bash
kubectl -n renfield apply -f k8s/alembic-upgrade-job.yaml
kubectl -n renfield wait --for=condition=Complete job/alembic-upgrade --timeout=300s
kubectl -n renfield logs job/alembic-upgrade
kubectl -n renfield delete job alembic-upgrade
```

The job uses the same backend image; if you just pushed a new image, run migrations **before** the deployment restart so the new code doesn't hit an old schema. The Job's `backoffLimit: 2` means 3 attempts max — failures are usually a chain conflict (see check above) or a missed env var, not a transient retry-able problem.

### ConfigMap changes

When `config/mcp_servers.yaml` / `config/agent_roles.yaml` / `config/kg_scopes.yaml` / `config/mail_accounts.*.yaml` change in the repo, rewrite the `renfield-mcp-config` ConfigMap **before** the rolling restart — otherwise pods get stuck in `ContainerCreating` on a missing subPath:

```bash
kubectl -n renfield create configmap renfield-mcp-config \
  --from-file=config/mcp_servers.yaml \
  --from-file=config/agent_roles.yaml \
  --from-file=config/kg_scopes.yaml \
  --from-file=mail_accounts.yaml=config/mail_accounts.default.yaml \
  --dry-run=client -o yaml | kubectl apply -f -
kubectl -n renfield rollout restart deploy/backend
```

### Smoke test

```bash
curl -sk https://renfield.local/health   # {"status":"ok"}
curl -sI http://renfield.local/          # 308 → https
```

## End-to-end checklist (run through this every release)

1. ✅ Merge release commits into `main` (PR review done).
2. ✅ Tag `vX.Y.Z` locally + push tag + create GitHub release.
3. ✅ rsync `src/backend`, `src/frontend`, `data/wakeword-models` to `/tmp/renfield-build-vX.Y.Z` on .159 (the model dir lands INSIDE the backend build context as `wakeword-models/`).
4. ✅ Verify staging — `Dockerfile`, `.dockerignore`, `wakeword-models/` (~9 files) all present; `config/mcp_servers.yaml` etc. NOT present (else the configmap mount breaks).
5. ✅ Build backend (long if requirements.txt changed) and frontend (fast).
6. ✅ Push `:latest` and `:vX.Y.Z` for both — verify each `digest:` line in the push output.
7. ✅ `kubectl rollout restart` on backend, dlna-mcp, document-worker, frontend (all four).
8. ✅ `kubectl rollout status` per deploy with 600s timeout.
9. ✅ Verify image digests across all 4 deploys match what was pushed.
10. ✅ Backend health smoke (`curl -sS http://localhost:8000/health` inside the pod).
11. ✅ Browser smoke for migrated pages / new features.
12. ✅ Cleanup `/tmp/renfield-build-vX.Y.Z` on .159.

## Build-box testing (not production)

The .159 build box runs the full Docker Compose stack for integration testing. It's where pytest lives because CI doesn't run.

```bash
# Rsync the working copy to the build box (exclude secrets + scratch)
rsync -avz --exclude='.env' --exclude='secrets/' --exclude='.git' \
  --exclude='node_modules' --exclude='__pycache__' --exclude='tasks/' \
  /Users/evdb/projects.ai/renfield/ 192.168.1.159:/opt/renfield/

# Run the suite inside the backend container
#   Source mount:  /opt/renfield/src/backend → /app
#   Tests mount:   /opt/renfield/tests       → /tests
ssh 192.168.1.159 "docker exec renfield-backend python -m pytest /tests/backend/... -q"
```

For a narrow-scope rsync of a few files, per-file `rsync` calls matching the repo layout are usually faster than a full tree sync. See `memory/reference_test_runner_159.md` for the full test-runner workflow including the "filter by subsystem + compare against pre-change tip via `git stash`" pattern used to distinguish regressions from the ~161 pre-existing failures.

**Do NOT** treat the build-box Compose stack as production. It runs without GPU, has no Harbor pull credentials plumbed in, and is routinely reset during testing.

## See also

- `docs/KUBERNETES_DEPLOYMENT.md` — canonical manifest + cluster-inventory reference
- `references/secrets.md` — Docker secrets + Harbor pull secret
- `references/docker-variants.md` — Compose variants (dev vs. build-box vs. retired prod compose)
- `references/satellite-deploy.md` — Satellite safety & provisioning (Ansible playbook, SD-card brick risk, 4-mic HAT gotchas)
