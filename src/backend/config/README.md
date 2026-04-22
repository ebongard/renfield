# `src/backend/config/`

Backend-internal config shipped inside the image (`COPY . .` in `src/backend/Dockerfile`).

## What lives here

- `context_extraction.yaml` — LLM extraction prompts for context vars
- `mcp_compact.yaml` — MCP response compaction rules
- `i18n/` — backend translations

## What does NOT live here

`agent_roles.yaml`, `mcp_servers.yaml`, `kg_scopes.yaml`, `mail_accounts.yaml`,
and `calendar_accounts.yaml` are **runtime config** owned by the top-level
[`config/`](../../../config/) directory.

Both deployment modes overlay them onto `/app/config/` at runtime:
- **Docker Compose:** bind-mount declared in `docker-compose.yml`
- **Kubernetes:** the `renfield-mcp-config` ConfigMap mounted at
  `/app/config/{mcp_servers,agent_roles,kg_scopes,mail_accounts}.yaml`

The duplicates that used to live here drifted from the canonical top-level
copies and silently broke a deploy in 2026-04 (#436, #437). Don't bring them
back. If you need to add a new top-level config key, add it under
[`config/`](../../../config/) and ensure both Docker Compose's bind-mount
list and the k8s ConfigMap include it.

## What if I run the bare image without overlay?

You shouldn't, but if you do, the backend will fail at startup with
`FileNotFoundError: config/agent_roles.yaml`. That's intentional — running
without one of the overlays would silently fall back to a stale snapshot,
which is exactly the bug we eliminated.
