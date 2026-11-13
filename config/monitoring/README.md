# Renfield monitoring: Grafana dashboards

Three dashboards for the Prometheus metrics the backend exports at `/metrics`.
They read nothing else: no logs, no database, no other exporter.

| File | Dashboard | Shows |
|---|---|---|
| `grafana/dashboards/renfield-overview.json` | Renfield / Overview | scrape targets up, MCP health verdict and monitor heartbeat, open circuit breakers, HTTP rate / 5xx share / latency, open WebSocket connections, logins, authorization denials, guard hits, voice turns without text |
| `grafana/dashboards/renfield-llm-agent.json` | Renfield / LLM and agent | duration and outcome of the agent loop's LLM calls, truncated answers, agent run outcomes and steps, router fallbacks, embedding errors, orchestrator domain coverage |
| `grafana/dashboards/renfield-integrations.json` | Renfield / Integrations and knowledge | MCP tool calls per server by outcome and duration, timeout share (what the health verdict counts), RAG search duration, memory and knowledge graph gauges |

## Status

Loaded once into a throwaway Grafana 13.3 with a Prometheus 3.16 scraping the
real `utils.metrics` (2026-11-13): provisioning is clean and every query
evaluates, on a freshly started backend and with samples flowing. What that
load showed and no structural check had seen (texts that a graph panel never
draws, shares that read "No data" while nothing failed, a reducer that hid NaN
behind an older value) is fixed and pinned in
`tests/backend/test_grafana_dashboards.py`, next to the checks that need no
Grafana: the JSON structure, that every series and label in a panel exists in
the backend's metric registry, and that every such series has a recorder that
is called. Change the JSON in git, not in the UI.

## Prerequisites

1. `METRICS_ENABLED=true` on the backend (see `docs/ENVIRONMENT_VARIABLES.md`,
   "Monitoring"). Without it there is no `/metrics` route and nothing is
   recorded.
2. A Prometheus that scrapes the backend. `/metrics` has no authentication and
   is reachable only inside the cluster or compose network (the ingress does
   not route it), so Prometheus has to run there too:

   ```yaml
   scrape_configs:
     - job_name: renfield
       static_configs:
         - targets: ['backend:8000']   # the backend service of the instance
   ```

   One Prometheus can scrape several instances; give each its own `job_name`.
   Every panel is filtered by the dashboard's `Job` variable.
3. A Prometheus data source in Grafana. The dashboards do not name one; they
   use the `Data source` variable.

Only the backend process exports metrics. The document, meeting and PDF-split
workers run the same code but serve no `/metrics`, so work done there (document
ingestion, for example) is not on these dashboards. The MCP servers export
nothing of their own; what is shown about them is measured from the backend.

## Loading the dashboards

**File provisioning** (dashboards follow the repository):

- mount `grafana/provisioning/dashboards/renfield.yaml` into
  `/etc/grafana/provisioning/dashboards/`,
- mount `grafana/dashboards/` at `/var/lib/grafana/dashboards/renfield`
  (the `path` in that provider file).

For example, as volumes of a Grafana container:

```yaml
volumes:
  - ./config/monitoring/grafana/provisioning/dashboards/renfield.yaml:/etc/grafana/provisioning/dashboards/renfield.yaml:ro
  - ./config/monitoring/grafana/dashboards:/var/lib/grafana/dashboards/renfield:ro
```

Grafana picks the files up at start and re-reads them every 60 s. They appear
in the folder "Renfield".

**Manual import**: Dashboards → New → Import, upload one JSON file, then pick
the Prometheus data source in the `Data source` drop-down at the top of the
dashboard.

No Grafana or Prometheus deployment is part of this repository.

## How the panels relate to "healthy"

The dashboards do not define health a second time.

- **MCP servers with a problem** is the count the MCP health monitor itself
  computed from `MCPManager.get_status()`: the verdict the kiosk and
  `/api/mcp/status` show. It needs `MCP_HEALTH_MONITOR_ENABLED=true`. Until a
  tick has reached a verdict the gauge is NaN and the panel reads "no verdict",
  never 0. After a first verdict the number is the last one: if the monitor
  then fails on every tick, ticks keep counting and the old number stays. Which
  server it is, and why, is in `/api/mcp/status`; there is no per-server status
  metric.
- The same holds for **Active memories** and **Knowledge graph conflation
  candidates**: NaN ("not counted yet", "no scan yet", in orange) until their
  job first ran in this backend process, so an instance without the job never
  shows a clean zero it did not measure. Both are stat panels with the history
  as a line behind the number. Conflation candidates are green at 0 and red
  from 1; the number of active memories is a count and carries no colour.
- These three panels show the NEWEST sample, NaN included: after a backend
  restart they read "no verdict" / "not counted yet" / "no scan yet" again
  until the job has run, they do not keep the number from before the restart.
- A share (**Share of 5xx responses**, **Share of tool calls that timed out,
  by server**, **Share of runs that did not end in a final answer**) is 0 %
  while requests, calls or runs happen and none of them failed, and every
  server that was called has a line. No line means nothing happened in the
  window, so there is no share to state.
- **Circuit breaker state** is one band per breaker (closed, open, half open;
  "unknown" for a state the gauge has no number for). A breaker exports its
  state only when it changes: one that never opened has no band, and the time
  before a first change is "no state reported". While no breaker has changed
  state at all the panel has nothing to draw and Grafana says "Data does not
  have a time field"; **Open circuit breakers** reads 0 then.
- The rate panels on counters that carry a label (failed logins, denials,
  guard hits, router fallbacks, embedding errors, tool calls, RAG searches)
  read "No data" until the first such event of a backend process: the series
  does not exist before. That is not drawn as a zero line, because a zero
  without the label has no name in the legend and would stay beside the real
  series afterwards.
- **MCP health monitor ticks** is red at 0: monitor off or stuck.
- **Tool calls by outcome** separates `timeout` from `app_error` because the
  verdict does: only timeouts count against a server (`calls_failing`). A
  device that is switched off produces `app_error` and is not an outage.
  `error` is every call that raised: a protocol error the server answered with
  (`McpError`) and a died session share that value and cannot be told apart
  here.
- Calls to federation peers are deliberately not observed: a peer's name is
  personal and must not become a `server` label value.
- Tool calls include the monitor's own functional probes.
- Readiness of the backend stays with `/health/ready`; the dashboards show the
  scrape state (`up`) only.

## What is deliberately missing

- **Per-user activity** (messages per user, voice against text): no metric
  carries a user, session or device identity, and none will. A label like that
  is unbounded and personal.
- **Intent accuracy**: there is no feedback signal to compute it from.
- **Token throughput**: no metric exports token counts.
- **Durations of LLM calls outside the agent loop** (router, intent, memory
  extraction, RAG answers): not recorded. The LLM panels say "agent loop" for
  that reason.
- **Per-tool breakdown of MCP calls**: the metric is labelled by server and
  outcome only; tool names come from the remote server.

## Changing a dashboard

Edit the JSON and run `tests/backend/test_grafana_dashboards.py`. A panel that
names a series or label the backend does not register fails there, as does a
quantile that is not built on a `_bucket` series or a rate window other than
`$__rate_interval`. A metric renamed in `src/backend/utils/metrics.py` fails the
same test until the panels follow.
