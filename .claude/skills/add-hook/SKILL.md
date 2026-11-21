---
name: add-hook
description: Guide for extending Renfield via the async hook system. Create plugins that inject LLM context, post-process messages, add routes, or register agent tools. Triggers on "add hook", "Hook erstellen", "Plugin schreiben", "extend Renfield", "register_hook", "PLUGIN_MODULE".
---

# Extending Renfield via Hooks

Minimal async hook system for the Open-Core plugin architecture. External packages register async callbacks at well-defined lifecycle points. Errors in hooks are logged but never crash Renfield.

## Quick Start

### 1. Create a plugin module

```python
# my_plugin/hooks.py
from utils.hooks import register_hook

async def _on_post_message(user_msg, assistant_msg, user_id, session_id, **kw):
    # Post-process every chat exchange
    ...

async def _on_retrieve_context(query, user_id, lang, **kw):
    # Inject additional LLM context
    return "## My Context\n- ..."

def register():
    register_hook("post_message", _on_post_message)
    register_hook("retrieve_context", _on_retrieve_context)
```

### 2. Set environment variable

```bash
PLUGIN_MODULE=my_plugin.hooks:register
```

### 3. Done

Hooks are called automatically at defined insertion points.

## Key Rules

- Hook functions **must be `async`**
- `retrieve_context` hooks should return `str` (or `None` to skip)
- `post_message` runs as fire-and-forget — don't block
- `register_tools` adds custom tools to the Agent Loop
- `register_routes` mounts additional FastAPI routers
- All hook events are whitelisted in `HOOK_EVENTS` — typos raise `ValueError`
- Key file: `utils/hooks.py`

## Hook Events (Quick Reference)

| Event | Purpose |
|-------|---------|
| `startup` | Initialize extension services |
| `shutdown` | Clean up resources |
| `register_routes` | Mount FastAPI routes |
| `register_tools` | Add agent tools |
| `post_message` | Post-process chat exchanges |
| `post_document_ingest` | Process ingested documents |
| `retrieve_context` | Inject LLM context |
| `presence_enter_room` | User entered room |
| `presence_leave_room` | User left room |
| `presence_first_arrived` | First user detected |
| `presence_last_left` | Last occupant left |
| `post_login_granted` | A login was granted (password, voice, SSO) — notification only |

## See Also

- `references/hook-events.md` — All events with full kwargs
- `references/insertion-points.md` — Where hooks fire in the code
