# Renfield test instance on Host D (LXC 101 `renfield-test`)

A complete Renfield instance for testing, self-contained on Host D (Proxmox, started and
stopped regularly, never part of the cluster). The GPU tier runs next door in LXC 100
`gpu-ct` (`../llama-server.service`, `../ollama.service`, `../voice-server.service`); this
directory is the rest: backend, the three workers, frontend, Postgres, Redis, SearXNG, an
own Paperless, and Caddy as the ingress. Issue #1636.

**Nothing here may reach a production system or a real person.** `overrides.env` switches
off every integration that writes, notifies or controls something real (HA, n8n, mail,
Jellyfin, DLNA, Samsung, scanner, files/folder ingest, push, notifier, presence/BT,
satellite enrollment, federation reachability, watchdog). Paperless is the test container
in this project, never the household VM.

| What | Where |
|---|---|
| UI | `https://renfield-test.local` (mDNS from avahi in the CT; Caddy internal CA) |
| Test Paperless | `http://renfield-test.local:8000` (user `admin`) |
| Renfield admin login | `/root/renfield-test-admin` on the CT (root-only) |
| Paperless admin password, all other secrets | `/opt/renfield-test/run/secrets.env` (0600) |
| Runtime dir (compose project) | `/opt/renfield-test/run` |
| Repo copy it is rendered from | `/opt/renfield-test/src` |

## Configuration = production + overrides

`render.py` writes `base.env` from the `renfield-env` ConfigMap in `k8s/configmap.yaml`,
so the test instance runs the production settings. `overrides.env` (committed) makes it
the test instance; `secrets.env` (generated on the box) carries the secrets. Compose reads
them in that order; the later file wins. Change a setting for the test box ONLY in
`overrides.env` — `base.env` is overwritten on every render.

## Install (once)

1. CT: unprivileged Debian 13, `features: nesting=1,keyctl=1`, 14 GB RAM, 120 GB, bridge
   `vmbr1` (DHCP), `startup: order=2` (after `gpu-ct`). Docker CE + compose plugin,
   `python3-yaml`, `avahi-daemon`, locale `de_DE.UTF-8`.
2. Registry: `192.168.1.1  registry.treehouse.x-idra.de` in `/etc/hosts`
   (`private_k8s/docs/harbor-registry-internal-pin.md`), `docker login` with the pull-only
   robot account `robot$renfield+hostd-pull` (token via stdin, never on the command line).
3. Repo copy: from a checkout,
   `rsync -aR k8s/configmap.yaml k8s/searxng.yaml config/mcp_servers.yaml config/agent_roles.yaml config/mail_accounts.default.yaml deploy/hostd/renfield-test/ root@renfield-test.local:/opt/renfield-test/src/`.
   `kg_scopes.yaml` is not in git:
   `kubectl -n renfield get cm renfield-mcp-config -o jsonpath='{.data.kg_scopes\.yaml}' | ssh root@renfield-test.local 'cat > /opt/renfield-test/run/config/kg_scopes.yaml'`.
4. Render with fresh secrets:
   `python3 /opt/renfield-test/src/deploy/hostd/renfield-test/render.py /opt/renfield-test/src /opt/renfield-test/run --init --backend-image <reg>/renfield/backend:<tag> --frontend-image <reg>/renfield/frontend:<tag>`.
5. `docker compose up -d postgres redis searxng paperless`, then create the Paperless API
   token (`manage.py shell`: `Token.objects.get_or_create(user=admin)`) and write it into
   `PAPERLESS_API_TOKEN=` in `secrets.env` without printing it.
6. `docker compose up -d`. The backend builds the schema on the empty database itself
   (`init_db` → baseline migration). First login as `admin` with `DEFAULT_ADMIN_PASSWORD`
   from `secrets.env` forces a password change (`/api/auth/change-password`).
7. Clients: trust Caddy's root once —
   `docker compose exec caddy cat /data/caddy/pki/authorities/local/root.crt`.

## Update

- Config: refresh the repo copy (step 3), rerun `render.py` without `--init`, `docker compose up -d`.
- Images: `bin/update-compose-instance.sh --target root@renfield-test.local --dir /opt/renfield-test/run --compose-file docker-compose.yml --backend-tag <tag> --frontend-tag <tag> [--migrate]`
  (backup, migration gate, rollout, health check — the same gate as the k8s deploy).

## Traps (each one happened during the setup, 2026-11-23)

- The rate limiter has its own Redis URL (`API_RATE_LIMIT_STORAGE_URI`); with only
  `REDIS_URL` carrying the password every request is a 500 from the limiter. In k8s the
  `redis-auth` Secret sets both.
- The workers get NO healthcheck (like k8s): the image HEALTHCHECK probes `:8000`, which
  only the backend serves, so they showed "unhealthy" while working.
- The image CMD runs uvicorn with `--reload` (dev); the backend service sets the prod args.
- "in meinen Dokumenten" routes to Paperless search, "in der Wissensdatenbank" to the
  knowledge base — an empty test Paperless answers "nothing found". Not a defect.
