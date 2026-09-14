# MCP Server YAML Fields Reference

All fields for `config/mcp_servers.yaml` server entries.

## Required Fields

| Field | Type | Description |
|-------|------|-------------|
| `name` | string | Server identifier, used in `mcp.<name>.<tool>` namespace |
| `transport` | enum | `streamable_http`, `sse`, or `stdio` |
| `enabled` | string | Env-var toggle, e.g. `"${MY_ENABLED:-false}"` |

## Connection Fields

| Field | Type | Description |
|-------|------|-------------|
| `url` | string | Server URL (for `streamable_http` / `sse`) |
| `command` | string | Executable (for `stdio`, e.g. `npx`, `python`) |
| `args` | list | Command arguments (for `stdio`) |
| `env` | dict | Environment variables passed to stdio subprocess |
| `refresh_interval` | int | Tool list refresh interval in seconds (default: 300) |
| `call_timeout` | number or dict | Tool-call timeout override in seconds (1..3600, env-substitutable). A number applies to every tool of the server; a mapping `{tool_name: seconds, default: seconds}` lets ONE slow tool run long without stretching its siblings. Omit = global `MCP_CALL_TIMEOUT` (30 s). The HTTP transport read timeout follows the longest value automatically. Prefer a job model (tool returns at once, result reported later) over long timeouts for anything that takes minutes — see the scanner. |

## Prompt Fields

| Field | Type | Description |
|-------|------|-------------|
| `prompt_tools` | list | Tool base names to include in LLM intent prompt. Omit = show all. All tools remain executable. |
| `example_intent` | string | Override intent name in prompt examples. Defaults to first tool. |
| `examples` | dict | Bilingual example queries: `{de: [...], en: [...]}` |

## Permission Fields

| Field | Type | Description |
|-------|------|-------------|
| `permissions` | list | Server-level permission strings, e.g. `["mcp.calendar.read", "mcp.calendar.manage"]`. User needs at least one. |
| `tool_permissions` | dict | Per-tool permission mapping, e.g. `{list_events: "mcp.calendar.read"}`. Takes priority over server-level. |

## Notification Fields

| Field | Type | Description |
|-------|------|-------------|
| `notifications` | dict | Proactive polling config: `{enabled: true, poll_interval: 900, tool: "get_pending_notifications"}`. Requires `NOTIFICATION_POLLER_ENABLED=true`. |

## Complete Example

```yaml
servers:
  - name: calendar
    url: "${CALENDAR_MCP_URL:-http://localhost:9095/mcp}"
    transport: streamable_http
    enabled: "${CALENDAR_ENABLED:-false}"
    refresh_interval: 300
    prompt_tools:
      - list_events
      - create_event
    example_intent: mcp.calendar.list_events
    examples:
      de: ["Was steht heute an?", "Termine diese Woche"]
      en: ["What's on today?", "Events this week"]
    permissions: ["mcp.calendar.read", "mcp.calendar.manage"]
    tool_permissions:
      list_events: "mcp.calendar.read"
      create_event: "mcp.calendar.manage"
    notifications:
      enabled: true
      poll_interval: 900
      tool: get_pending_notifications
```
