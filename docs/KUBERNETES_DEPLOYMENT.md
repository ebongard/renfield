# Kubernetes Deployment

Renfield on the private K8s cluster (`renfield-private` context) with GPU-accelerated LLM inference.

## Cluster Inventory

| Node | IP | Role | GPU |
|------|-----|------|-----|
| k8s-cp | 192.168.1.213 | Control plane | — |
| k8s-gpu-1 | 192.168.1.180 | Worker | 1× NVIDIA |
| k8s-gpu-2 | 192.168.1.148 | Worker | 1× NVIDIA |

**Infrastructure:**

- K8s v1.35.3, containerd, Calico CNI
- Storage: Longhorn (default `longhorn` SC, 3 replicas on the two worker disks)
- Load Balancer: MetalLB L2 mode, IP pool `192.168.1.230–240`; Traefik LB at `192.168.1.230` on ports 80 and 443
- Ingress: Traefik v3.3 with both the `kubernetesingress` and `kubernetescrd` providers enabled (lives in the sibling `private_k8s/` repo)
- GPU: NVIDIA device plugin DaemonSet, `nvidia.com/gpu: 1` per worker
- LLM models: NFS `192.168.1.9:/mnt/data/llm` → `/mnt/llm` on both workers, hostPath-mounted into Ollama pods

Cluster-wide Traefik changes (entrypoints, TLS, CRDs) are tracked in `../private_k8s/traefik.yaml` and `../private_k8s/traefik-crds.yaml`; this document only covers the renfield-namespace workload.

## Architecture

```
                        LAN (192.168.1.0/24)
                               │
                 ┌─────────────┼─────────────┐
          mDNS announce       LB IP       Satellites / browsers
       renfield.local  ←──  .230  ──►  wss://renfield.local/ws/satellite
                               │              https://renfield.local/
                               ▼
                         ┌───────────┐
                         │  Traefik  │ :80 → RedirectScheme → :443
                         │  (ingress)│ :443 → TLS (renfield-tls)
                         └─────┬─────┘
                  /api,/ws,/health │ /
                  ┌───────────────┬┴─────────┐
                  ▼               ▼          ▼
           ┌────────────┐  ┌───────────┐  ┌──────────┐
           │  Backend   │  │  Frontend │  │ Traefik  │
           │  (FastAPI) │  │  (Nginx)  │  │  serves  │
           │   :8000    │  │    :80    │  │  static  │
           └─┬────┬────┬┘  └───────────┘
             │    │    │
      ┌──────┘    │    └──────────┐
      │           │               │
      ▼           ▼               ▼
 ┌────────┐  ┌────────┐     ┌──────────┐
 │Postgres│  │ Redis  │     │  Ollama  │
 │ (PVC)  │  │ (PVC)  │     │ 2 pods,  │
 └────────┘  └────────┘     │ 1 GPU ea │
                            └──────────┘
      ▲           ▲
      │ http      │ streamable-http (9091/mcp)
  SearXNG    ┌────┴───────┐
  (cluster)  │ DLNA-MCP   │ hostNetwork → SSDP multicast on LAN
             └────────────┘
             ┌────────────┐
             │ mDNS       │ hostNetwork → publishes renfield.local
             │ Responder  │ to LAN (singleton)
             └────────────┘
```

## Container Images

| Service | Image | Notes |
|---------|-------|-------|
| Backend | `registry.treehouse.x-idra.de/renfield/backend:latest` | CPU image (~3.5 GB). Includes wake-word models and renfield-mcp-dlna entrypoint |
| Frontend | `registry.treehouse.x-idra.de/renfield/frontend:latest` | Nginx serving React SPA |
| PostgreSQL | `pgvector/pgvector:pg16` | pgvector for embedding search |
| Redis | `redis:7-alpine` | Message queue + cache (AOF enabled) |
| Ollama | `ollama/ollama:latest` | LLM inference, requires GPU |
| SearXNG | `searxng/searxng:latest` | In-cluster metasearch |
| DLNA-MCP | reuses backend image | Runs `/opt/venv/bin/renfield-mcp-dlna` as main entrypoint |
| mDNS responder | `debian:bookworm-slim` | Installs avahi at container start; small, runs once per lifecycle |

Harbor is the registry. A `harbor-pull-secret` of type `kubernetes.io/dockerconfigjson` lives in the renfield namespace and is referenced by every pod that pulls from there.

### Why torch+cpu via constraints

The backend image had ballooned to 7.5 GB because transitive deps in `docling`, `easyocr`, and `transformers` upgraded torch to a CUDA build, dragging in `nvidia/*` wheels (2.7 GB) and `triton` (641 MB). `src/backend/constraints.txt` pins `torch`/`torchaudio`/`torchvision` to `+cpu` wheels from the PyTorch CPU index so pip's resolver cannot upgrade. Final image: ~3.5 GB, which pushes through Harbor's proxy without the 504 timeouts that the old layer had.

## Manifest Structure

```
k8s/
├── namespace.yaml                  Namespace renfield
├── secrets.yaml.example            12 app secrets + harbor-pull-secret (TEMPLATE)
├── configmap.yaml                  Env vars (endpoints, models, feature flags)
├── postgres.yaml                   StatefulSet + Service + PVC
├── redis.yaml                      Deployment + Service + PVC
├── ollama.yaml                     Deployment (2 replicas, GPU-only workers,
│                                    anti-affinity) + Service + model pre-pull Job
├── searxng.yaml                    Deployment + Service + settings ConfigMap
├── dlna-mcp.yaml                   hostNetwork Deployment + ClusterIP Service
├── mdns-responder.yaml             hostNetwork Deployment (Avahi singleton)
├── backend.yaml                    Deployment + Service + PVC; mounts the
│                                    renfield-mcp-config ConfigMap over
│                                    /app/config/{mcp_servers,agent_roles,kg_scopes}.yaml
├── frontend.yaml                   Deployment + Service
├── ingress.yaml                    Two Ingresses: renfield-http (redirect) and
│                                    renfield-https (TLS termination + routing)
├── middleware-https-redirect.yaml  Traefik Middleware CRD (namespace-scoped)
├── alembic-upgrade-job.yaml        Opt-in Job (NOT applied by default) for
│                                    incremental migrations on an existing DB
└── kustomization.yaml              `kubectl apply -k k8s/`
```

## Bootstrap vs. Migration — Fresh Install Story

Renfield's database bootstrap is **not** `alembic upgrade head`. Old prod used `Base.metadata.create_all()` at startup to materialise the schema from SQLAlchemy models, and applied individual alembic migrations later on top of an already-populated DB. The 41-migration history cannot replay from an empty DB because the baseline revision (`9a0d8ccea5b0`) is an empty stub — all subsequent migrations assume a rich pre-existing schema.

For K8s this means:

- The backend bootstraps itself on first boot: `init_db()` → `Base.metadata.create_all()` → `_ensure_alembic_baseline()` stamps `alembic_version` to the current head if empty.
- **No `migrate` init container** on the backend deployment. Adding one that runs `alembic upgrade head` on a fresh DB would re-expose the broken chain.
- Incremental migrations (when a new version of the backend ships a new migration) are applied by hand, using the opt-in `k8s/alembic-upgrade-job.yaml`:

  ```bash
  kubectl -n renfield apply -f k8s/alembic-upgrade-job.yaml
  kubectl -n renfield logs -f job/alembic-upgrade
  kubectl -n renfield delete job alembic-upgrade
  ```

A follow-up that consolidates all 41 migrations into a single clean baseline is tracked separately.

## Secrets

The `renfield-secrets` Secret holds:

| Key | Purpose |
|-----|---------|
| `postgres-password` | PostgreSQL password |
| `secret-key` | FastAPI session signing key |
| `default-admin-password` | Initial admin password (surface once; rotate on first login) |
| `home-assistant-token` | HA long-lived access token |
| `openweather-api-key` | OpenWeather API key (for the weather MCP) |
| `newsapi-key` | NewsAPI |
| `jellyfin-api-key` / `jellyfin-token` / `jellyfin-base-url` / `jellyfin-user-id` | Jellyfin |
| `n8n-api-key` | n8n workflow API |
| `paperless-api-token` | Paperless-ngx |
| `mail-regfish-password` | Email (Regfish) |

Apply imperatively with real values rather than committing `secrets.yaml`:

```bash
kubectl -n renfield create secret generic renfield-secrets \
  --from-literal=postgres-password="…" \
  --from-literal=secret-key="$(openssl rand -hex 32)" \
  …
```

## ConfigMap

Environment variables in `renfield-env` (excerpt):

```yaml
DATABASE_URL: postgresql+asyncpg://renfield:…@postgres:5432/renfield
OLLAMA_URL: http://ollama:11434
REDIS_URL: redis://redis:6379
DLNA_MCP_URL: http://dlna-mcp:9091/mcp
SEARXNG_API_URL: http://searxng:8080
HOME_ASSISTANT_URL: http://192.168.1.80:8123
JELLYFIN_URL: http://192.168.1.123:8096
OLLAMA_CHAT_MODEL: qwen3:14b
OLLAMA_EMBED_MODEL: qwen3-embedding:4b
# + feature flags (AGENT_ENABLED, MCP_ENABLED, *_MCP_ENABLED, RAG_ENABLED, …)
# + satellite version (SATELLITE_LATEST_VERSION)
```

A separate ConfigMap `renfield-mcp-config` provides the runtime versions of `mcp_servers.yaml`, `agent_roles.yaml`, `kg_scopes.yaml`, and `mail_accounts.yaml` mounted at `/app/config/`. The image no longer ships duplicates of these files (#437) — the ConfigMap is the only source on the cluster, the top-level `config/` directory is the only source in the repo. Rebuild the ConfigMap from `config/*.yaml` whenever the top-level files change.

## TLS / HTTPS

- Self-signed cert generated locally (CN=`renfield.local`, SANs include `*.renfield.local` and `192.168.1.230`) stored as `renfield-tls`.
- Traefik's `websecure` entrypoint on `:443` is configured cluster-wide (`private_k8s/traefik.yaml`).
- `renfield-http` ingress matches only the `web` entrypoint and attaches the `https-redirect` Middleware (`RedirectScheme { scheme: https, permanent: true }`).
- `renfield-https` ingress matches only `websecure` and carries the `spec.tls` block + `router.tls: "true"` annotation.
- Cluster-wide HTTP→HTTPS would break other ingresses (Longhorn, Traefik dashboard) that serve plain `.test.local` hosts — hence the per-ingress middleware approach.

A follow-up will introduce cert-manager with Let's Encrypt.

## LAN DNS

Satellites and browsers resolve `renfield.local` via multicast DNS. The `mdns-responder` pod runs Avahi on a worker's host network and publishes `renfield.local → 192.168.1.230`; it is a singleton (only one Avahi per name on the LAN).

For clients whose resolvers bypass mDNS (e.g., `getent hosts renfield.local` via DNS-only on some systems), the site's upstream DNS also carries the record.

## Deploy Sequence

```bash
# 1. Namespace
kubectl apply -f k8s/namespace.yaml

# 2. Harbor pull secret (from a docker config with harbor auth)
cat ~/.docker/config.json | kubectl -n renfield create secret generic \
  harbor-pull-secret --type=kubernetes.io/dockerconfigjson \
  --from-file=.dockerconfigjson=/dev/stdin

# 3. App secrets (real values)
kubectl -n renfield create secret generic renfield-secrets \
  --from-literal=postgres-password=… \
  --from-literal=secret-key="$(openssl rand -hex 32)" \
  …

# 4. TLS secret (self-signed for now)
openssl req -x509 -nodes -newkey rsa:2048 -keyout tls.key -out tls.crt -days 3650 \
  -subj "/CN=renfield.local/O=Renfield" \
  -addext "subjectAltName=DNS:renfield.local,DNS:*.renfield.local,IP:192.168.1.230"
kubectl -n renfield create secret tls renfield-tls --cert=tls.crt --key=tls.key
rm tls.{crt,key}

# 5. MCP / agent config ConfigMap
# `mail_accounts.default.yaml` is renamed to `mail_accounts.yaml` in the
# ConfigMap — the mail MCP expects that exact filename. Swap in a real
# accounts file here if you have one.
kubectl -n renfield create configmap renfield-mcp-config \
  --from-file=config/mcp_servers.yaml \
  --from-file=config/agent_roles.yaml \
  --from-file=config/kg_scopes.yaml \
  --from-file=mail_accounts.yaml=config/mail_accounts.default.yaml

# 6. Everything else via kustomize
kubectl apply -k k8s/

# 7. Watch rollout
kubectl -n renfield get pods -w

# 8. Smoke test
curl -sk https://renfield.local/health          # → {"status":"ok"}
curl -sI http://renfield.local/                 # → 308 → https
```

## Updating

For a new backend image:

```bash
kubectl -n renfield rollout restart deploy/backend
# or pin a specific version
kubectl -n renfield set image deploy/backend backend=registry.treehouse.x-idra.de/renfield/backend:v1.3.1
```

When the new version contains migrations:

```bash
kubectl -n renfield apply -f k8s/alembic-upgrade-job.yaml
kubectl -n renfield logs -f job/alembic-upgrade
kubectl -n renfield delete job alembic-upgrade
```

When the new version changes the MCP config set (new file in
`config/`, new ConfigMap key referenced by a manifest), the
`renfield-mcp-config` ConfigMap has to be rewritten before the
manifests are applied — otherwise the pods get stuck in
`ContainerCreating` on a missing subPath. The pattern:

```bash
kubectl -n renfield create configmap renfield-mcp-config \
  --from-file=config/mcp_servers.yaml \
  --from-file=config/agent_roles.yaml \
  --from-file=config/kg_scopes.yaml \
  --from-file=mail_accounts.yaml=config/mail_accounts.default.yaml \
  --dry-run=client -o yaml | kubectl apply -f -
kubectl -n renfield rollout restart deploy/backend
```

When retiring a PVC that's attached to a running Deployment (e.g. the
`renfield-data` removal post-#388), roll the Deployment first so the
PVC becomes unbound, then delete it:

```bash
kubectl apply -k k8s/                         # manifest no longer mounts it
kubectl -n renfield rollout status deploy/backend
kubectl -n renfield delete pvc renfield-data  # now unbound
```

Ollama model pulls: drop the model into the NFS share at `192.168.1.9:/mnt/data/llm/.ollama/` and it becomes visible to both Ollama pods; the `ollama-model-prepull` Job can also be re-applied to pull a specific list.

## Not Yet Included

- **cert-manager / real TLS certs** — current deploy uses self-signed
- **Migration chain consolidation** — the 41-migration history is bypassed by `Base.metadata.create_all` for fresh installs; a clean baseline that replaces the empty stub is still pending
- **HPA** — horizontal scaling for backend
- **NetworkPolicy** — pod-to-pod traffic restrictions
- **Backup** — Longhorn snapshot schedule for the postgres PVC
- **Evolution API / WhatsApp** — optional profile
- **Satellite management CRDs** — rollout orchestration for Pi Zero fleet
