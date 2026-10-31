# `src/backend/config/`

Backend-internal config shipped inside the image (`COPY . .` in `src/backend/Dockerfile`).

## What lives here

- `context_extraction.yaml` — JSON-path rules that pull follow-up context vars
  (last document id, last email, …) out of MCP tool results; no LLM involved
- `mcp_compact.yaml` — MCP response compaction rules (field whitelists per tool)
- `i18n/` — backend translations

Both YAMLs are written against the CURRENT response shape of each MCP tool, and a
path into an older shape matches nothing — silently. That is how every agent
Paperless search reached the LLM as `{}` ("nothing found") for months after
renfield-mcp-paperless wrapped its list into `{"summary", "results"}`. So:

- Every rule has a recorded response shape (`RECORDED_SHAPES`, built from the MCP
  source, invented values) in `tests/backend/test_mcp_compact.py` /
  `test_context_extractor.py`; the tests load the shipped YAML and fail for a rule
  without a shape. When an MCP pin in `requirements.txt` or the `n8n-mcp` pin in
  the Dockerfile moves, re-check the shapes against the new source.
- Extraction runs on the COMPACTED result: a compaction rule must keep every field
  an extraction rule reads.
- A compaction rule that reduces a non-empty payload to nothing falls back to the
  uncompacted payload with a WARNING
  `MCP compaction rule for <tool> kept nothing …`. That line in the backend log
  means the rule no longer matches its tool. "Truncated" holds for the prompt:
  the MCP client cuts `message`, the agent loop cuts message and data to
  `AGENT_RESPONSE_TRUNCATION`.
- The MCP client's `_truncation` note (it cut the largest array to its size
  limit) is kept by every rule, so a cut list never reads as the whole answer.

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
