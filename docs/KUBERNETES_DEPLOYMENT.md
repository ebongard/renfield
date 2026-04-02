# Kubernetes Deployment

Renfield on a private K8s cluster with GPU-accelerated LLM inference.

## Cluster Inventory

| Node | IP | Role | GPU |
|------|-----|------|-----|
| k8s-cp | 192.168.1.213 | Control plane | — |
| k8s-gpu-1 | 192.168.1.180 | Worker | 1x NVIDIA |
| k8s-gpu-2 | 192.168.1.148 | Worker | 1x NVIDIA |

**Infrastructure:**

- K8s v1.35.3, containerd, Calico CNI
- Storage: Longhorn (default SC, 3 replicas)
- Load Balancer: MetalLB L2 mode, IP pool 192.168.1.230–240
- Ingress: Traefik v3.3 (standard Kubernetes Ingress resources), LB at 192.168.1.230:80
- GPU: NVIDIA device plugin DaemonSet, `nvidia.com/gpu: 1` per worker
- LLM models: NFS `192.168.1.9:/mnt/data/llm` → `/mnt/llm` on both workers (Ollama blobs/manifests)

## Architecture

```
                    ┌──────────────┐
                    │   Traefik    │  192.168.1.230:80
                    │   (Ingress)  │
                    └──────┬───────┘
                           │
              ┌────────────┼────────────┐
              │            │            │
         /api/* , /ws      │           /
              │            │            │
        ┌─────▼─────┐     │     ┌──────▼──────┐
        │  Backend   │     │     │  Frontend   │
        │  (FastAPI) │     │     │  (Nginx)    │
        │  port 8000 │     │     │  port 80    │
        └─────┬──────┘     │     └─────────────┘
              │            │
     ┌────────┼────────┐   │
     │        │        │   │
┌────▼───┐ ┌──▼──┐ ┌──▼───▼──┐
│Postgres│ │Redis│ │ Ollama   │
│  5432  │ │6379 │ │ 11434   │
│ (PVC)  │ │(PVC)│ │ 2 pods  │
└────────┘ └─────┘ │ 1 GPU ea│
                   └─────────┘
```

## Container Images

| Service | Image | Notes |
|---------|-------|-------|
| Backend | `ghcr.io/ebongard/renfield/backend:latest` | CPU image — calls Ollama via HTTP |
| Frontend | `ghcr.io/ebongard/renfield/frontend:latest` | Nginx serving React SPA |
| PostgreSQL | `pgvector/pgvector:pg16` | pgvector for embedding search |
| Redis | `redis:7-alpine` | Message queue + cache |
| Ollama | `ollama/ollama:0.15.1` | LLM inference, requires GPU |

Images are published by the release pipeline (`.github/workflows/release.yml`) on tag push (`v*.*.*`).

## Manifest Structure

```
k8s/
├── namespace.yaml          # Namespace: renfield
├── secrets.yaml            # 12 secrets (template — fill before applying)
├── configmap.yaml          # Env vars + mounted YAML configs
├── postgres.yaml           # StatefulSet + Service + PVC (Longhorn)
├── redis.yaml              # Deployment + Service + PVC (Longhorn)
├── ollama.yaml             # Deployment (2 replicas, anti-affinity) + Service
├── backend.yaml            # Deployment + Service + init container (migrations)
├── frontend.yaml           # Deployment + Service
├── ingress.yaml            # Kubernetes Ingress for Traefik
└── kustomization.yaml      # kubectl apply -k k8s/
```

## Service Specifications

### PostgreSQL

| | |
|---|---|
| Kind | StatefulSet |
| Image | `pgvector/pgvector:pg16` |
| Replicas | 1 |
| Storage | PVC 10Gi (Longhorn) |
| Service | ClusterIP `postgres:5432` |
| Liveness | `pg_isready -U renfield` |
| Resources | req 256Mi / 250m, lim 1Gi / 1000m |
| Env | `POSTGRES_DB=renfield`, `POSTGRES_USER=renfield`, password from Secret |

### Redis

| | |
|---|---|
| Kind | Deployment |
| Image | `redis:7-alpine` |
| Replicas | 1 |
| Command | `redis-server --appendonly yes` |
| Storage | PVC 2Gi (Longhorn) |
| Service | ClusterIP `redis:6379` |
| Liveness | `redis-cli ping` |
| Resources | req 64Mi / 100m, lim 256Mi / 500m |

### Ollama

| | |
|---|---|
| Kind | Deployment |
| Image | `ollama/ollama:0.15.1` |
| Replicas | 2 |
| Scheduling | `podAntiAffinity` hard rule on hostname — one pod per GPU node |
| GPU | `resources.limits: nvidia.com/gpu: 1` |
| Models | hostPath `/mnt/llm` → `/root/.ollama` (NFS, already mounted on workers) |
| Env | `OLLAMA_MODELS=/root/.ollama` |
| Liveness | exec `ollama list` (timeout 10s) |
| Readiness | HTTP GET `/` on port 11434 |
| Service | ClusterIP `ollama:11434` (load-balances across both pods) |
| Resources | req 4Gi / 1000m + 1 GPU, lim 16Gi / 4000m + 1 GPU |

### Backend

| | |
|---|---|
| Kind | Deployment |
| Image | `ghcr.io/ebongard/renfield/backend:latest` |
| Replicas | 1 |
| Init container | Same image, `alembic upgrade head` (DB migrations) |
| Liveness | HTTP GET `/health` on port 8000 |
| Readiness | Same |
| Service | ClusterIP `backend:8000` |
| Resources | req 512Mi / 500m, lim 2Gi / 2000m |
| Env (Secret) | DATABASE_URL, SECRET_KEY, DEFAULT_ADMIN_PASSWORD, HOME_ASSISTANT_TOKEN, integration API keys |
| Env (ConfigMap) | OLLAMA_URL, REDIS_URL, feature flags |
| Volumes | ConfigMap → `/app/config/` (mcp_servers.yaml, agent_roles.yaml, kg_scopes.yaml) |

### Frontend

| | |
|---|---|
| Kind | Deployment |
| Image | `ghcr.io/ebongard/renfield/frontend:latest` |
| Replicas | 1 |
| Liveness | nginx PID check |
| Service | ClusterIP `frontend:80` |
| Resources | req 64Mi / 50m, lim 256Mi / 200m |

Build args `VITE_API_URL` and `VITE_WS_URL` are baked into the image at release time.

### Ingress

Standard Kubernetes Ingress resource consumed by Traefik:

| Path | Backend | Port | Notes |
|------|---------|------|-------|
| `/api` | backend | 8000 | API routes |
| `/ws` | backend | 8000 | WebSocket (Traefik handles upgrade natively) |
| `/health` | backend | 8000 | Health check |
| `/` | frontend | 80 | React SPA (catch-all) |

Host: `renfield.local` (or whatever DNS record points to 192.168.1.230).

Annotation: `traefik.ingress.kubernetes.io/router.entrypoints: web`

## Secrets

12 values stored in a Kubernetes Secret (`renfield-secrets`):

| Key | Source |
|-----|--------|
| `postgres-password` | PostgreSQL password |
| `secret-key` | FastAPI session signing key |
| `default-admin-password` | Initial admin password |
| `home-assistant-token` | Home Assistant long-lived access token |
| `openweather-api-key` | OpenWeather API |
| `newsapi-key` | NewsAPI |
| `jellyfin-api-key` | Jellyfin API key |
| `jellyfin-token` | Jellyfin auth token |
| `jellyfin-base-url` | Jellyfin server URL |
| `n8n-api-key` | n8n workflow API key |
| `paperless-api-token` | Paperless-ngx API token |
| `mail-regfish-password` | Email (Regfish) password |

`secrets.yaml` is a template with `CHANGEME` placeholders. Fill before applying. **Never commit real values.**

## ConfigMap

Environment variables and mounted configuration files:

```yaml
# Environment variables
OLLAMA_URL: "http://ollama:11434"
REDIS_URL: "redis://redis:6379"
HOME_ASSISTANT_URL: "http://homeassistant.local:8123"
DEFAULT_LANGUAGE: "de"
AUTH_ENABLED: "true"
AGENT_ENABLED: "true"
MCP_ENABLED: "true"
MEMORY_ENABLED: "true"
MEMORY_EXTRACTION_ENABLED: "true"
RAG_ENABLED: "true"
LOG_LEVEL: "INFO"
DO_NOT_TRACK: "1"
```

Mounted YAML files (from `config/` in repo):
- `mcp_servers.yaml` — MCP server definitions
- `agent_roles.yaml` — Agent role config
- `kg_scopes.yaml` — Knowledge Graph scopes

## Deploy Sequence

```bash
# 1. Create namespace
kubectl apply -f k8s/namespace.yaml

# 2. Fill secrets with real values
cp k8s/secrets.yaml k8s/secrets-real.yaml
# Edit k8s/secrets-real.yaml with actual values (base64-encoded)
kubectl apply -f k8s/secrets-real.yaml

# 3. Deploy everything
kubectl apply -k k8s/

# 4. Watch rollout
kubectl -n renfield get pods -w

# 5. Verify
curl http://192.168.1.230/health
curl http://192.168.1.230/api/health
```

The backend init container runs `alembic upgrade head` before the main process starts, ensuring the database schema is current.

## Updating

```bash
# Pull latest images and restart
kubectl -n renfield rollout restart deployment/backend
kubectl -n renfield rollout restart deployment/frontend

# Or for a specific version
kubectl -n renfield set image deployment/backend backend=ghcr.io/ebongard/renfield/backend:v1.3.0
```

For Ollama model updates, add models directly to the NFS share at `192.168.1.9:/mnt/data/llm`. Both Ollama pods read from the same path.

## Not Yet Included

- **TLS/HTTPS** — add cert-manager + Let's Encrypt or self-signed certs when needed
- **Evolution API** — WhatsApp integration (optional profile)
- **Satellite management** — Pi Zero 2 W OTA updates
- **HPA** — Horizontal Pod Autoscaling for backend
- **NetworkPolicy** — Pod-to-pod traffic restrictions
- **Backup** — Longhorn snapshot schedule for PostgreSQL PVC