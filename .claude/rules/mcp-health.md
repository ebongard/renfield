---
paths:
  - "src/backend/services/mcp_health_monitor.py"
  - "src/backend/services/mcp_client.py"
  - "src/backend/services/health_check.py"
  - "config/mcp_servers.yaml"
  - "src/backend/api/routes/mcp_health.py"
  - "src/backend/services/search_health.py"
---
# MCP self-detection, functional probes, k8s health routes

Loaded only when the MCP health monitor, the MCP client, `health_check.py` or `mcp_servers.yaml` is read.
Long form + failure-mode catalog: `docs/design/mcp-self-detection.md`. The alert path is `services/ops_alert.py`
(shared with scheduled tasks + watchdog, see `scheduled-tasks.md`); `_notify`/`_should_alert` are thin delegates.

Flags: `MCP_HEALTH_MONITOR_ENABLED` dark (delivery needs `PROACTIVE_ENABLED`) · `MCP_HEALTH_SELF_HEAL_ENABLED` on ·
`MCP_HEALTH_PROBE_ENABLED` on (the throttle is the YAML, not the flag) · `MCP_HEALTH_RATE_LIMIT_SIGNAL_ENABLED` dark ·
`MCP_RATE_LIMIT_BACKOFF_ENABLED` **on in the household since 2026-09-21** (BL-0416: only the backoff — a tool's
Retry-After is honoured, no automatic retry; signal + alert stay dark).

## Verdict (`_server_health`, fed by `MCPManager.get_status()`)
- **`degraded/calls_failing` counts ONLY timeouts** (rolling window on `MCPServerState`: clean result = ok, timeout =
  fail). App-level `isError`/not-found/device-off envelopes are NEVER counted — an app error says nothing about the
  SERVER. A correctness decision; do not reverse it — its blind spots are closed by probes, not by loosening.
- Fold order: `probe_failed` → `rate_limited` → `calls_failing` (direct evidence beats inference).
- Tick order: get_status → self-heal (`probe_server()`) → re-read → **probe** → re-read → the ONE alert pass.
- `no_tools`/`plugin_failed`/`rate_limited` are `_UNHEALABLE_CODES` — not self-heal-probed: `tools/list` answers
  fine with `[]`, so a reconnect "recovers" a tool-less server.
- `execute_tool_streaming` shares `execute_tool`'s accounting (caller cancellation records nothing; app error ≠
  disconnect). Connect + `refresh_tools` install tools ONLY through `_install_discovered_tools`.
- **Error result** = `_detect_inner_error`, TOP level only: a `success`/`ok` BOOLEAN decides alone, else an `error`
  string or a TRUTHY `error_code`. `ok` was missing until #1367, so our own servers' `{"ok": false, …}` passed as a
  success into `action_success`. Never recurse — `{"ok": true, "result": {"ok": false}}` is a REPORT about a failure.

## Functional probes (`health_probe:` stanza: `tool`/`args`/`interval`/`timeout`/`expect.{min_items,path}`, parsed like `notifications:`)
- Verdict = `probe_consecutive_failures`, kept SEPARATE from `recent_outcomes`; hysteresis
  `MCP_HEALTH_PROBE_FAIL_THRESHOLD` (2). New `impaired_code: probe_failed` (localized).
- **A reconnect does NOT clear a probe verdict** — it proves the transport, nothing about the service.
- **`search` deliberately has NO stanza** (a bare result count is a documented false-green). It keeps
  `services/search_health.py` (#1162) via `_BESPOKE_PROBES` + `record_external_probe`; `unknown` records nothing.
- Skipped on purpose: `per_user_auth` servers (a `user_id=None` call is denied fail-closed → permanent false
  failure), federation, disconnected, every server without a stanza.
- 🛑 **A probe must not fire on the NORMAL state, or it gets switched off.** `scanner` had no stanza until
  2026-10-21 because "the operator Mac is asleep" reads as `down` — true, and already covered by the
  disconnected skip. But the same reasoning covered a second case it should not have: service up, button dead.
  On 2026-10-21 the blue button was dead for NINE DAYS with a live supervisor, green liveness and
  `device_readable: true`. The stanza now probes `watcher_health`, NOT `scanner_status`: the latter touches the
  device and returns an error envelope for a switched-off scanner (the normal case). `watcher_health` reads only
  free things (the watcher's timestamp, `ioreg`) — so it cannot wedge the device it measures — and errors ONLY on
  watcher dead/crashloop or a WEDGE (USB node present, SANE blind). A missing `ioreg` answer is never a finding.
- Probes run as system calls (`user_permissions=None`/`user_id=None`) so they never colour `ToolOutcomeStat`.
- 🛑 **Jeder stdio-Server mit `command: npx` muss im Bild auf eine EXAKTE Version festgenagelt sein.** Fehlt er
  dort, lädt `npx -y <paket>` ihn beim POD-START aus dem Netz und führt ihn aus (so lief `mcp-newsapi@^1.0.0`
  in Produktion); steht er mit `@latest` drin, entscheidet der Bildbau. Beides holt eine Verhaltensänderung ohne
  eine Zeile Diff herein. Am 2026-10-26 so passiert: `n8n-mcp` prüft die API-BASISADRESSE durch seinen
  SSRF-Riegel und weist private Adressen ab, n8n muss aber im LAN stehen → alle 13 Werkzeuge tot, Transport
  weiter „verbunden", nur die Sonde sah es. Schalter `WEBHOOK_SECURITY_MODE: permissive` in der Stanza (er räumt
  Loopback, private Bereiche und private IPv6 ab; die Metadaten-Sperrliste und die Schema-Prüfung bleiben in
  JEDEM Modus). Die Pflicht wird aus der Konfiguration hergeleitet, nicht aus dem Dockerfile:
  `test_n8n_mcp_ssrf_and_pin.py`. Hergang: `docs/design/mcp-self-detection.md`.

## Kommt der Steuertext überhaupt an?
- 🛑 **`tool_hints` können konfiguriert sein und das Modell trotzdem NIE erreichen** — am 2026-10-26 taten sie
  das vier Review-Runden lang. `log_hint_arrival` (gerufen in `_install_discovered_tools`, NACH dem Filtern)
  zählt je Entdeckung: konfiguriert / angehängt / **angeboten**. Die Differenz ist der Punkt — ein
  `prompt_tools`-Filter oder eine DB-Übersteuerung kann ein behinweistes Werkzeug aus `state.tools` werfen.
- 🛑 **Abwesenheit ist keine Diagnose:** es gibt IMMER eine Zeile, auch `0 hint(s) configured`. Sonst sehen drei
  Lagen gleich aus — Server ohne Hinweise, Konfiguration ohne, Bild ohne die Funktion.
- Nur bei ÄNDERUNG, weil die Entdeckung alle ~60 s läuft (`mcp_refresh_interval`; `refresh_tools` liest den
  per-Server-Wert NICHT). Nur Namen und Längen, nie der Hinweistext.
- 🛑 Der Schlüssel ist der ROHE Werkzeugname (`scan_document`), nicht der namensräumliche. Hergang:
  `docs/design/mcp-self-detection.md`.

## Alerting
- "Told" = **a notification row exists.** `notify_admin` returns `True` for a failure AFTER `process_webhook`
  committed the row (fresh-session check `_persisted_since`); only a MISSING row is retried, after
  `MCP_HEALTH_ALERT_RETRY_SECONDS` (600, `ops_alert.defer_alert`). Never discard `notify_admin`'s bool.
- Alerts go through `notify_admin`, which vouches them as technical for the LLM steps (`llm_eligible`; type gate
  `PROACTIVE_LLM_EVENT_TYPES` must list `mcp_health`). Plane-B `reason`/`root` are relayed text — keep them short.
- Re-alert key = server+health: `planea:{name}:{health}`. A reason flap inside the TTL does not re-alert. The recovery
  sweep forgets a server's keys only when it has no problem at all (re-arm only, no MCP recovery notice).
- No boot storm: `MCPServerState.no_tools_since` (NOT reset by a reconnect that still finds nothing) →
  `no_tools_for_seconds` → the alert waits `MCP_HEALTH_NO_TOOLS_GRACE_SECONDS` (300); the verdict shows at once.

## Rate limit
- `_classify_rate_limit` reads ONLY error results/app exceptions (`429 Too Many Requests`, JSON status 429, "rate
  limit"). **A bare "429" never counts** — fix the MCP at the source (return `status: 429` + `retry_after`), never
  loosen the classifier. → `rate_limit_events`, separate from the timeout window and the probe verdict;
  `…_MIN_EVENTS` 5 in `…_WINDOW_SECONDS` 900. A throttled probe records no verdict but advances its cadence.
- Retry-After = per-TOOL fail-fast until the horizon (cap `MCP_RATE_LIMIT_MAX_BACKOFF_SECONDS`). **NO transparent
  retry** (a 429 mid-tool can follow side effects). Our own refusal is not a new event.

## k8s health routes (`services/health_check.py`)
- Liveness `/health/live` MUST stay dependency-free (a DB-dependent liveness restart-storms every replica).
- Readiness `/health/ready` gates on DB REACHABILITY (own NullPool engine, `HEALTH_READY_DB_TIMEOUT_SECONDS` 3 s),
  not pool saturation; Redis + device hook (`HEALTH_READY_AUX_TIMEOUT_SECONDS` 1 s) never 503.
- xidra's `backend.yaml` lives in `x-ren` and needs the same probe change.
