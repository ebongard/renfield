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

## Build + push workflow

Images live in Harbor and are pulled by the cluster via `imagePullPolicy: Always` on the `:latest` tag (or pinned tags like `:v1.3.1`).

Both Dockerfiles expect **their own directory as the build context** — not the repo root. The Dockerfile's `COPY requirements.txt constraints.txt ./` and `COPY wakeword-models /app/wakeword-models` resolve against context root, and those files live at `src/backend/*`, not at the repo root.

```bash
# From the build box (.159). You need to be logged in:
docker login registry.treehouse.x-idra.de

# Backend (CPU image, ~3.5 GB — torch pinned to +cpu wheels via constraints.txt)
cd /opt/renfield/src/backend
docker build -t registry.treehouse.x-idra.de/renfield/backend:latest \
             -t registry.treehouse.x-idra.de/renfield/backend:pr<N>-<sha> \
             -f Dockerfile .
docker push registry.treehouse.x-idra.de/renfield/backend:latest
docker push registry.treehouse.x-idra.de/renfield/backend:pr<N>-<sha>

# Frontend (Nginx serving React build, ~144 MB)
cd /opt/renfield/src/frontend
docker build -t registry.treehouse.x-idra.de/renfield/frontend:latest -f Dockerfile .
docker push registry.treehouse.x-idra.de/renfield/frontend:latest
```

Always build + push a pinned tag (`pr<N>-<sha>`) alongside `:latest` — gives you an immutable rollback target (`kubectl set image deploy/backend backend=.../backend:pr<N>-<sha>`).

**Why the image stays ~3.5 GB:** `src/backend/constraints.txt` pins `torch`/`torchaudio`/`torchvision` to the `+cpu` wheels so transitive deps (docling, easyocr, transformers) can't drag in the 2.7 GB CUDA runtime + 641 MB triton. Don't lift that constraint unless you've thought through the Harbor push timeout.

## Cluster rollout

```bash
kubectl config use-context renfield-private

# Rolling restart to pull a new :latest
kubectl -n renfield rollout restart deploy/backend
kubectl -n renfield rollout restart deploy/document-worker
kubectl -n renfield rollout restart deploy/frontend

# Or pin an explicit tag
kubectl -n renfield set image deploy/backend \
  backend=registry.treehouse.x-idra.de/renfield/backend:v1.3.2

# Watch the rollout
kubectl -n renfield get pods -w
```

### Migrations

```bash
kubectl -n renfield apply -f k8s/alembic-upgrade-job.yaml
kubectl -n renfield logs -f job/alembic-upgrade
kubectl -n renfield delete job alembic-upgrade
```

The job uses the same backend image; if you just pushed a new image, run migrations before the deployment restart so the new code doesn't hit an old schema.

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
