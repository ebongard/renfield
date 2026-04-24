# Reva: Private Repo on Renfield Platform

## Goal

Port Roberta's features (Teams bot, LDAP, Release management) onto the Renfield Python codebase. Ship as **Reva** — a private repo that uses Renfield as its foundation. Retire the Node.js Roberta codebase.

---

## Architecture: Reva as Plugin on Renfield

Same pattern as renfield-twin: private repo that extends Renfield via the hook system.

```
reva/                              (PRIVATE repo)
├── renfield/                      (git submodule → public renfield repo)
├── src/
│   └── reva/
│       ├── __init__.py
│       ├── hooks.py               # Entry point: PLUGIN_MODULE=reva.hooks:register
│       ├── teams_transport.py     # Teams bot adapter (botbuilder-core Python SDK)
│       ├── teams_auth.py          # Teams user ↔ Release user mapping (5-pass)
│       ├── ldap_service.py        # LDAP group member resolution
│       ├── release_roles.py       # Role/team resolution chain (5-level lookup)
│       ├── notify_handler.py      # POST /api/notify webhook from Release plugin
│       ├── subscriptions.py       # User subscribes to release events
│       ├── system_prompt.md       # Reva personality & tool instructions
│       └── config.py              # Reva-specific settings (extends Renfield's)
├── config/
│   └── mcp_servers.yaml           # Release MCP server definition
├── release-plugin/                # Java plugin for Digital.ai Release (copied from Roberta)
│   ├── pom.xml
│   └── src/.../RobertaNotifyListener.java
├── docker-compose.yml             # Extends renfield's compose (adds Release MCP container)
├── Dockerfile                     # FROM renfield base, adds reva layer
├── .env.example
├── README.md
└── tests/
    └── test_*.py                  # Reva-specific tests
```

### How It Connects

```
Renfield (public)                    Reva (private plugin)
┌─────────────────────┐              ┌─────────────────────────┐
│ FastAPI app          │◄── hooks ───│ reva.hooks:register()   │
│ Agent loop           │              │                         │
│ MCP framework        │              │ register_routes:        │
│ Conversations        │              │   POST /api/messages    │
│ Auth / RBAC          │              │   POST /api/notify      │
│ RAG / Memory         │              │                         │
│ Hook system          │              │ register_tools:         │
│                      │              │   resolve_team_members  │
│ edition=pro:         │              │   resolve_role          │
│   smart_home OFF     │              │   list_global_roles     │
│   satellites OFF     │              │   manage_subscriptions  │
│   presence OFF       │              │                         │
└─────────────────────┘              │ startup:                │
                                     │   init Teams adapter    │
                                     │   connect Release MCP   │
                                     └─────────────────────────┘
```

### Hook Registrations

```python
# reva/hooks.py
from utils.hooks import register_hook

def register():
    register_hook("startup", on_startup)
    register_hook("register_routes", register_reva_routes)
    register_hook("register_tools", register_reva_tools)
    register_hook("shutdown", on_shutdown)

async def register_reva_routes(app, **kwargs):
    from reva.teams_transport import teams_router
    from reva.notify_handler import notify_router
    app.include_router(teams_router, prefix="/api/teams")
    app.include_router(notify_router, prefix="/api/notify")

async def register_reva_tools(registry, **kwargs):
    from reva.release_roles import role_tools
    from reva.subscriptions import subscription_tools
    for tool in role_tools + subscription_tools:
        registry.register(tool)
```

### Teams Message Flow

```
Teams User → Azure Bot Service → POST /api/teams/messages
  → teams_transport.py (Bot Framework Python SDK)
    → teams_auth.py (map Teams user → Release user → Renfield user)
    → Renfield's agent_service.process_message()
      → Agent loop with Release MCP tools + LDAP tools
    → Send response back via Teams adapter
```

---

## Feature Migration Map (Roberta JS → Reva Python)

| Roberta (JS) | Reva (Python) | Effort | Notes |
|---------------|---------------|--------|-------|
| `agentLoop.js` (114 LOC) | **Not needed** — use Renfield's agent service | 0 | Core Renfield feature |
| `mcpClient.js` (82 LOC) | **Not needed** — use Renfield's MCP manager | 0 | Just add Release MCP to config |
| `ollamaClient.js` (26 LOC) | **Not needed** — use Renfield's LLM client | 0 | Core Renfield feature |
| `toolConverter.js` (16 LOC) | **Not needed** — Renfield handles conversion | 0 | Core Renfield feature |
| `conversationStore.js` (40 LOC) | **Not needed** — use Renfield's conversations | 0 | Core Renfield feature |
| `bot.js` (124 LOC) | `teams_transport.py` — new Teams adapter | **3 days** | `botbuilder-core` Python SDK |
| `releaseAuth.js` (151 LOC) | `teams_auth.py` — user mapping | **2 days** | 5-pass matching logic |
| `releaseRoles.js` (510 LOC) | `release_roles.py` — role resolution tools | **3 days** | Most complex piece |
| `ldapClient.js` (77 LOC) | `ldap_service.py` — group resolution | **1 day** | `python-ldap` or `ldap3` |
| `notifyHandler.js` (145 LOC) | `notify_handler.py` — webhook receiver | **1 day** | FastAPI route + Teams proactive msg |
| `referenceStore.js` (140 LOC) | `subscriptions.py` — DB-backed subscriptions | **2 days** | SQLAlchemy model instead of JSON file |
| `toolLabels.js` (71 LOC) | Renfield's i18n system | 0 | Already handled |
| `config.js` (53 LOC) | `config.py` — extend Renfield settings | **0.5 day** | Pydantic Settings subclass |
| `SystemMessage.md` (169 LOC) | `system_prompt.md` — Reva personality | **0.5 day** | Adapt, don't rewrite |
| `RobertaNotifyListener.java` | **Copy as-is** — just update webhook URL | **0.5 day** | Same Java plugin |

**Total migration effort: ~2 weeks**

---

## Renfield Changes Required (in public repo)

Small changes needed in Renfield to support Reva (and future plugins):

| Change | Reason | Impact |
|--------|--------|--------|
| `register_routes` hook must receive the FastAPI `app` instance | Reva needs to mount its own routers | ~5 lines in `main.py` |
| `register_tools` hook must receive the tool registry | Reva registers LDAP/role tools | ~5 lines in agent setup |
| Agent service needs a transport-agnostic message interface | Currently assumes WebSocket response; Teams needs HTTP response | ~20 lines refactor |
| Add `PLUGIN_MODULE` loading at startup (if not already wired) | Plugin entry point | Already exists, verify it works |

**Total Renfield changes: ~30 lines, non-breaking**

---

## Repo Setup

### Step 1: Create private repo
```bash
mkdir reva && cd reva
git init
git submodule add git@github.com:ebongard/renfield.git renfield
```

### Step 2: Docker setup
```dockerfile
# Dockerfile
FROM renfield:latest AS base
COPY src/reva /app/src/reva
COPY config/ /app/config/
ENV PLUGIN_MODULE=reva.hooks:register
ENV RENFIELD_EDITION=pro
```

```yaml
# docker-compose.yml (extends renfield's)
services:
  reva:
    build: .
    env_file: .env
    depends_on: [postgres, redis]
    environment:
      - PLUGIN_MODULE=reva.hooks:register
      - RENFIELD_EDITION=pro

  release-mcp:
    image: xebialabsearlyaccess/dai-release-mcp:25.3.0-beta.926
    environment:
      - XLR_URL=${RELEASE_BASE_URL}
      - XLR_TOKEN=${RELEASE_TOKEN}
```

### Step 3: Development workflow
```bash
# Update Renfield to latest
cd reva/renfield && git pull origin main && cd ..
git add renfield && git commit -m "bump renfield submodule"

# Run locally
docker compose up -d

# Run Reva tests
python -m pytest tests/
```

---

## Implementation Order

### Phase 1: Foundation (Days 1-3)
- [ ] Create private `reva` repo with submodule structure
- [ ] Verify Renfield hook system supports route + tool registration
- [ ] Make minimal Renfield changes (~30 lines) for plugin support
- [ ] Dockerfile + docker-compose.yml for Reva
- [ ] `reva/hooks.py` — skeleton with all hook registrations
- [ ] `reva/config.py` — Reva-specific settings

### Phase 2: Teams Transport (Days 4-6)
- [ ] `teams_transport.py` — Bot Framework Python adapter
- [ ] `teams_auth.py` — Teams user ↔ Release user mapping
- [ ] Wire Teams messages → Renfield agent service → Teams response
- [ ] Test with Bot Framework Emulator

### Phase 3: Enterprise Tools (Days 7-10)
- [ ] `ldap_service.py` — group member resolution
- [ ] `release_roles.py` — role/team resolution (5-level chain)
- [ ] Register as agent tools via hook
- [ ] Connect Release MCP server (38 tools, zero porting needed)

### Phase 4: Notifications (Days 11-12)
- [ ] `notify_handler.py` — webhook receiver for Release events
- [ ] `subscriptions.py` — DB-backed subscription model
- [ ] Proactive Teams messaging (notify subscribed users/channels)
- [ ] Copy Java plugin, update webhook URL

### Phase 5: Polish & Deploy (Days 13-14)
- [ ] `system_prompt.md` — adapt Roberta's prompt for Reva
- [ ] Tests for all Reva-specific code
- [ ] Deploy to existing Roberta VM (192.168.99.41)
- [ ] Verify end-to-end: Teams → Reva → Release MCP → Teams response
- [ ] Retire Roberta Node.js

---

## Repo Map (All Variants)

```
PUBLIC:
  renfield/               (MIT)   — the platform, feature-flagged

PRIVATE:
  reva/                   (private) — Renfield + Teams + Release management
    └── renfield/         (submodule)

  renfield-twin/          (BSL)   — Renfield + KG + personality + twin mode
    └── renfield/         (submodule)
```

All three private products (Reva, Twin, eventually Professional) use the same pattern:
**git submodule + PLUGIN_MODULE hook + edition flag**

No code extraction needed. No fork maintenance. One Renfield, many plugins.
