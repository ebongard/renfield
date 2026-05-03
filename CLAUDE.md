# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

Renfield is a fully offline-capable, self-hosted **digital assistant** — a personal AI hub for knowledge retrieval, tool access, and smart home control. Serves multiple household users in parallel.

**Tech Stack:** Python 3.11 + FastAPI + SQLAlchemy | React 18 + TypeScript + Vite + Tailwind CSS + PWA | Docker Compose, PostgreSQL 16, Redis 7, Ollama | Satellites: Pi Zero 2 W + ReSpeaker + OpenWakeWord

**LLM:** Local models via Ollama (multi-model: chat, intent, RAG, agent, embeddings). See `docs/LLM_MODEL_GUIDE.md`.

**Integrations:** Home Assistant, Frigate, n8n, SearXNG, Jellyfin, DLNA, Paperless, Email, Calendar — all via MCP servers.

## KRITISCHE REGELN - IMMER BEACHTEN

**NIEMALS `git push` ohne explizite Erlaubnis des Benutzers ausfuehren!** Nach jedem Commit fragen: "Soll ich pushen?" Diese Regel gilt auch nach Session-Komprimierung. Details: `/git-workflow` Skill.

---

## Development Guidelines

### Test-Driven Development (TDD)

**WICHTIG: Bei jeder Code-Aenderung muessen passende Tests mitgeliefert werden.**

1. **Neue API-Endpoints**: Tests in `tests/backend/test_<route>.py` — HTTP status codes, schemas, error handling, edge cases
2. **Neue Services**: Tests in `tests/backend/test_services.py` — unit tests with mocks, `@pytest.mark.unit`
3. **Datenbank-Aenderungen**: Tests in `tests/backend/test_models.py` — model creation, constraints, `@pytest.mark.database`
4. **Frontend-Komponenten**: Tests in `tests/frontend/react/` — RTL rendering, user interactions, MSW API mocks

### Frontend Rules

- **TypeScript only — migration complete.** `src/frontend/src/` is 100% TS (49 .ts + 68 .tsx, 0 .jsx as of v2.4.3). `tests/frontend/react/` migrated alongside. Both have strict mode on with a `npm run typecheck` gate. Type real shapes — no `as any`, no `@ts-nocheck`, no shim files. The "fake-`.tsx` is worse than honest `.jsx`" rule from the W10 migration still applies to any future refactor that can't be typed cleanly in one pass.
- **DESIGN.md is the source of truth.** Before any UI change, read `DESIGN.md` at repo root. Color tokens, fonts, spacing, motion, semantic colors, and the tier visual language are defined there. Do NOT deviate without explicit user approval. In `/review` and `/qa`, flag any code that doesn't match DESIGN.md.
- **Dark Mode**: ALL components must use Tailwind `dark:` variants. Never hardcode colors.
- **i18n**: ALL user-facing strings must use `useTranslation()`. Never hardcode text.
- **Translations**: Add to BOTH `src/frontend/src/i18n/locales/de.json` and `en.json`.
- **Component classes** (in `index.css`): `.card`, `.input`, `.btn-primary`, `.btn-secondary`. New classes per DESIGN.md (e.g., `.tier-badge`, `.atom-row`) must use only DESIGN.md tokens.

## Development Commands

```bash
./bin/start.sh                  # Start entire stack
./bin/update.sh                 # Update system
./bin/debug.sh                  # Debug mode
./bin/quick-update.sh           # Quick backend restart
```

```bash
make lint                       # Lint all (ruff + eslint)
make format-backend             # Format + auto-fix with ruff
make test                       # Run all tests
make test-backend               # Backend tests only
make test-frontend-react        # React component tests (Vitest)
make test-coverage              # Coverage report (fail-under=50%)
```

```bash
docker exec -it renfield-backend alembic revision --autogenerate -m "description"
docker exec -it renfield-backend alembic upgrade head
docker exec -it renfield-backend alembic downgrade -1
```

**Configuration:** `pyproject.toml` — contains ruff, pytest, and coverage config.

## Architecture

**Request Flow:** User → React Frontend → WebSocket/REST → FastAPI Backend → Intent Recognition → Action Execution → MCP/RAG → Streaming Response

**Subsystems:** Intent Recognition, Agent Loop (ReAct), MCP Integration (8+ servers), RAG/Knowledge Base, Conversation Persistence, Hook System (plugin API), Auth/RPBAC, Presence Detection, Media Follow Me, Speaker Recognition, Knowledge Graph, Paperless Audit, Audio Output Routing, Notification Privacy, Device Management, **Circles (access tiers)**

**Key config:** All via `.env` loaded by `utils/config.py` (Pydantic Settings). Full list: `docs/ENVIRONMENT_VARIABLES.md`.

For architecture questions, use the `architecture-guide` agent.

### Platform-owned internal agent tools

The agent loop sees a mix of MCP tools (`mcp.<server>.<tool>`) and `internal.*` tools. Internal tools are platform-level wrappers that bundle multi-step workflows or chain MCP calls with real server-side state. Two live on the platform core (rest live in `ha_glue`):

| Tool | Purpose | Source |
|---|---|---|
| `internal.knowledge_search` | Semantic RAG search over the user's knowledge base | `services/knowledge_tool.py` |
| `internal.forward_attachment_to_paperless` | Forward a chat-attached file to Paperless using real server-stored bytes — prevents the LLM from handling base64 payloads it can't actually see | `services/chat_upload_tool.py` |

Dispatch for both is a special case in `services/action_executor.py` that injects dependencies the generic `intent.startswith("internal.")` hook path cannot provide (`mcp_manager`, `session_id`).

### Agent stale-error marker

Failed tool turns are persisted with `action_success: False` in message metadata. The `conv_context` builder in `services/agent_service.py` prepends `[VORHERIGE_FEHLGESCHLAGENE_AKTION]` to those assistant messages when re-injecting history into the next agent turn. The `conv_context_template` in `prompts/agent.yaml` carries a hint telling the LLM to treat marker lines as historical, not as current state — so a repeated user request retries the tool instead of echoing the old error.

### Circles v1 (access tiers)

Detailed user-facing and architectural documentation: [`docs/CIRCLES.md`](docs/CIRCLES.md). Narrative of the broader knowledge system (the four subsystems circles protect): [`docs/SECOND_BRAIN.md`](docs/SECOND_BRAIN.md). Code-level summary below.

Five-rung ladder on every source row that participates in retrieval:

| tier | name | meaning |
|---|---|---|
| 0 | self | owner-only |
| 1 | trusted | 1-3 closest people |
| 2 | household | family / housemates |
| 3 | extended | named outsiders |
| 4 | public | anyone |

Access to any source row = **OWNER** OR **tier == public** OR **explicit grant** (via `atom_explicit_grants`) OR **tier-reach through circle membership** (via `circle_memberships`). Retrieval modules (`rag_retrieval`, `kg_retrieval`, `memory_retrieval`) push this 4-branch filter into SQL via `services/circle_sql.py`. `AUTH_ENABLED=false` short-circuits the filter (single-user mode sees everything).

Key tables: `atoms` (polymorphic registry), `circles` (per-user dimension config), `circle_memberships`, `atom_explicit_grants`. Denormalized `circle_tier` + `atom_id` columns on `document_chunks`, `kg_entities`, `kg_relations`, `conversation_memories`.

Key services: `services/circle_resolver.py` (PolicyEvaluator + cache), `services/atom_service.py` (upsert + tier cascade), `services/polymorphic_atom_store.py` (cross-source RRF), `services/kb_shares_service.py` (KB-level share → per-chunk grant explosion), `services/circle_sql.py` (shared filter clause builder).

Key routes: `/api/atoms` (unified search + edit), `/api/circles/me/*` (settings, members, review queue), `/api/knowledge-graph/circle-tiers` (localized ladder labels), `/api/knowledge-graph/entities/{id}/circle-tier` (tier patch with cascade to incident relations).

Frontend pages: `/brain` (search), `/brain/review` (owner review queue), `/settings/circles` (members). Shared `TierBadge` + `TierPicker` components use `.tier-badge-{0..4}` utilities from `index.css`.

**Behavioral change vs pre-circles:** `ConversationMemoryService.retrieve()` now respects circle reach — tier-2 household peers see each other's household-tier memories. Previously `user_id == asker_id` filtered strictly. Flag in release notes.

For memory-retrieval callers: pass `user_id=asker_id`. For RAG search: pass `user_id=asker_id` in every `rag.search()` call — `None` reduces to public-tier-only in auth-enabled mode.

## Testing

Tests in `tests/` at project root. Backend: 1,300+ tests.

**Markers:** `@pytest.mark.unit`, `@pytest.mark.database`, `@pytest.mark.integration`, `@pytest.mark.e2e`, `@pytest.mark.backend`, `@pytest.mark.frontend`, `@pytest.mark.satellite`

**React tests:** Vitest + RTL + MSW in `tests/frontend/react/` (separate `package.json`, own `tsconfig.json`). `npm run typecheck` runs `tsc --noEmit` against the test files for compile-time validation; `npm test` runs vitest itself.

**Backend tests run on .159 build box, not in CI.** GitHub CI is intentionally non-functional for this project. See `memory/reference_test_runner_159.md` for the ssh/docker exec workflow.

## CI/CD Pipeline

| Workflow | Trigger | Reality |
|----------|---------|---------|
| `ci.yml` | Push to main/develop, PRs | **Non-functional** — kept for the audit trail; tests are run on `.159` instead |
| `pr-check.yml` | Pull requests | **Non-functional** — same |
| `release.yml` | Tag push (v*.*.*) | **Non-functional** — does NOT actually build images; tag is for git audit only |

The real release flow lives in `.claude/skills/deploy-production/SKILL.md`: build on `192.168.1.159`, push to Harbor at `registry.treehouse.x-idra.de`, kubectl rollout against the private cluster (context `renfield-private`). Backend image is multi-stage Dockerfile with split pip-install layers (Harbor proxy times out on >2.5 GB layers). Migrations: `kubectl -n renfield apply -f k8s/alembic-upgrade-job.yaml` BEFORE the rolling restart.

```bash
make release    # Create + push version tag — does NOT deploy. See deploy-production skill for the real flow.
```

## Skills & Agents

| Skill/Agent | Trigger | Purpose |
|-------------|---------|---------|
| `/git-workflow` | commit, push, PR, branch | Commit format, issue numbers, PR workflow |
| `/add-integration` | neue Integration, MCP server | Add MCP server to `mcp_servers.yaml` |
| `/add-hook` | Hook, Plugin, extend | Async hook system for plugins |
| `/add-frontend-page` | neue Seite, add page | Page creation, routing, navigation |
| `/deploy-production` | deploy, production, rsync | Docker deploy, secrets, satellites |
| `/debug-renfield` | debug, Fehler, broken | Troubleshooting all subsystems |
| `architecture-guide` | Architektur, how does X work | Read-only architecture Q&A (agent) |
| `satellite-deploy` | satellite deploy, provision Pi | Satellite deployment with safety rules (agent) |
| `test-runner` | run tests, pytest, vitest | Test execution and failure diagnosis (agent) |
