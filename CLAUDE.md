# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.
It holds ONLY what every session needs. Subsystem knowledge lives in path-scoped rules (`.claude/rules/*.md`, loaded
when a matching file is read — also in subagents) and in `docs/`. See the index at the end.

## Project Overview

Renfield is a fully offline-capable, self-hosted **digital assistant** — a personal AI hub for knowledge retrieval, tool access, and smart home control. Serves multiple household users in parallel.

**Tech Stack:** Python 3.11 + FastAPI + SQLAlchemy | React 18 + TypeScript + Vite + Tailwind CSS + PWA | Docker Compose, PostgreSQL 16 + pgvector, Redis 7, Ollama | Satellites: Pi Zero 2 W + ReSpeaker + OpenWakeWord (bare-metal via Ansible), plus one arm64 k8s-pod satellite (Orange Pi Zero 3W, Esszimmer).

**LLM:** Local models via Ollama (multi-model: chat, intent, RAG, agent, vision, embeddings).

**Integrations:** Home Assistant, Frigate, n8n, SearXNG, Jellyfin, DLNA, Samsung TV, Paperless, Email, Calendar, Filesystem (watch-folders), Parcel Tracking — all via MCP servers (`config/mcp_servers.yaml`). Servers that hold credentials the backend must not (filesystem, email-ingest) and the `hostNetwork` ones (DLNA, Samsung) run as their own deployments; the other stdio servers live in the backend image.

**Two instances:** household (`renfield`, `AUTH_ENABLED=false`) and xidra (`renfield-xidra`, auth on, config in the private `x-ren` repo). Every change must work on both.

## KRITISCHE REGELN - IMMER BEACHTEN

**NIEMALS `git push` ohne explizite Erlaubnis des Benutzers ausfuehren!** Nach jedem Commit fragen: "Soll ich pushen?" Diese Regel gilt auch nach Session-Komprimierung. Details: `/git-workflow` Skill.

**PR-Lifecycle-Gate: Nach `/review`, VOR dem Merge, IMMER ALLE relevanten Dokumentation aktualisieren.** Kein Nachfragen nötig — das ist Pflicht-Schritt, nicht optional. Sweep statt raten: `grep -rliE "<feature-begriffe>" docs/ README.md CLAUDE.md .claude/rules/` und jede betroffene Datei anpassen (typischerweise die Rule des Subsystems, `docs/CIRCLES.md`, `docs/SECOND_BRAIN.md`, `docs/FEATURES.md`). Doc-Update als eigener Commit in denselben PR, dann auf explizite Merge-Freigabe warten. Reihenfolge: `/review` → Docs aktualisieren → warten → merge.

**Neues Wissen gehört in die Rule des Subsystems oder in `docs/`, NICHT in diese Datei.** Diese Datei bleibt unter 200 Zeilen. Eine Rule hält nur Invarianten und Fallen (≤ 60 Zeilen); Historie und Begründungen gehören ins Design-Dokument.

---

## Development Guidelines

### Test-Driven Development (TDD)

**WICHTIG: Bei jeder Code-Aenderung muessen passende Tests mitgeliefert werden.**

1. **Neue API-Endpoints**: Tests in `tests/backend/test_<route>.py` — HTTP status codes, schemas, error handling, edge cases
2. **Neue Services**: Tests in `tests/backend/test_services.py` — unit tests with mocks, `@pytest.mark.unit`
3. **Datenbank-Aenderungen**: Tests in `tests/backend/test_models.py` — model creation, constraints, `@pytest.mark.database`
4. **Frontend-Komponenten**: Tests in `tests/frontend/react/` — RTL rendering, user interactions, MSW API mocks

A failing test is an issue, not noise: the backend suite is green, so investigate every red test. Tests encode the INTENDED behaviour — when behaviour changes on purpose, change the test to the new intent, never bend it to whatever the code does.

### Frontend Rules

- **TypeScript only — migration complete.** `src/frontend/src/` is 100% TS; `tests/frontend/react/` migrated alongside. Strict mode. Type real shapes — no `as any`, no `@ts-nocheck`, no shim files.
- **DESIGN.md is the source of truth.** Before any UI change, read `DESIGN.md` at repo root. Color tokens, fonts, spacing, motion, semantic colors, and the tier visual language are defined there. Do NOT deviate without explicit user approval. In `/review` and `/qa`, flag any code that doesn't match DESIGN.md. (Sanctioned exception: `/kiosk`.)
- **Dark Mode**: ALL components must use Tailwind `dark:` variants. Never hardcode colors.
- **i18n**: ALL user-facing strings must use `useTranslation()`. Never hardcode text.
- **Translations**: Add to BOTH `src/frontend/src/i18n/locales/de.json` and `en.json` (and `it.json` where the neighbouring keys exist).
- **Component classes** (in `index.css`): `.card`, `.input`, `.btn-primary`, `.btn-secondary`. New classes per DESIGN.md (e.g., `.tier-badge`, `.atom-row`) must use only DESIGN.md tokens.
- Never render model-generated HTML/SVG: typed JSON → real React components (the escape boundary is the security story).

### Definition of Done

1. Tests written and green, with the output shown — evidence, not claims.
2. Lint clean; frontend type-checks (see Testing for the real commands).
3. New feature = **dark by default** (`*_ENABLED=false`), flag-off path byte-identical. New env vars → `docs/ENVIRONMENT_VARIABLES.md`.
4. **A new `internal.*` tool is TWO steps:** `InternalToolService.TOOLS` + `_HANDLERS`, AND the tool name in the role's `internal_tools` list in `config/agent_roles.yaml` — which is ConfigMap-served (`renfield-mcp-config`), so the prod ConfigMap must be patched too. Skip step 2 and the agent reports "no tool available".
5. Security seams: new routes/tools are permission-gated and fail-closed when auth is on; `user_id`/`session_id` are injected server-side, never taken from LLM params; new retrieval rows carry `circle_tier` + `atom_id` and go through `circle_sql`; every retrieval call passes `user_id=asker_id`.
6. A live change that is not in git is a bug (manifests, ConfigMaps, image tags).
7. A migration runs BEFORE the rollout (`bin/deploy-production.sh … --migrate`): the ORM selects every column, so a new pod before the migration fails every query on that table. Never SSH-tail logs on a Pi Zero satellite (it reboots) — follow a voice turn in the backend + voice-server logs.

## Development Commands

```bash
./bin/start.sh                  # Start entire stack (build-box / dev compose)
./bin/quick-update.sh           # Quick backend restart
make lint                       # Lint all (ruff + eslint)
make format-backend             # Format + auto-fix with ruff
bin/deploy-production.sh …      # Real deploy (see the deploy-production skill; user-invoked)
```

**Configuration:** `pyproject.toml` (ruff, pytest, coverage). All settings via `.env` → `utils/config.py` (Pydantic Settings); full list `docs/ENVIRONMENT_VARIABLES.md`.

## Architecture

**Request Flow:** User → React Frontend → WebSocket/REST → FastAPI Backend → Intent Recognition → Action Execution → MCP/RAG → Streaming Response

For architecture questions use the `architecture-guide` agent; it reads `.claude/rules/` and `docs/design/`.

## Testing

Tests live in `tests/` at the project root (backend 3,400+). Markers: `@pytest.mark.unit`, `database`, `integration`, `e2e`, `backend`, `frontend`, `satellite`, `postgres`.

**There is no local Python test environment and GitHub CI does not run.** Reality:

- **Backend tests run on the build box `192.168.1.159`** inside the `renfield-backend` container, from an ISOLATED copy (never overlay `/opt/renfield`): rsync `src/backend` + `tests` + **`bin`** + **`config`** to `/tmp/<name>`, `docker cp` into the container, then `docker exec -w /<name>/src/backend -e PYTHONPATH=/<name>/src/backend renfield-backend python -m pytest /<name>/tests/backend/<file> -q -p no:cacheprovider -o asyncio_mode=auto`. `-o asyncio_mode=auto` is mandatory; without `bin` the script tests and without `config` the MCP tests error out on a missing file. Clean the copy up afterwards.
- **The test database is REAL POSTGRES, never sqlite.** Export `RENFIELD_TEST_PG_URL` pointing at the dedicated `renfield_test` DB (never a live one) — unset, the database tests SKIP rather than fall back. A sqlite harness forces dialect fallbacks into production code (`circle_sql` is Postgres SQL, the branch walk a recursive CTE, `search_vector` a GENERATED column) and a green run then proves the wrong system: it enforces no foreign keys, so tests asserted ownership between users that never existed. The harness builds the schema once per run, rebuilds the GENERATED columns the way the migrations do, and between tests empties only the tables that hold rows (~0.4 s instead of 1.8 s). Full suite: ~25 min.
- **Satellite tests** (`tests/satellite/`) need a throwaway venv (pytest, pytest-asyncio, pyyaml, numpy, websockets, cryptography, aiohttp); the suite runs in ~15 s.
- **React tests:** Vitest + RTL + MSW in `tests/frontend/react/` (own `package.json` + `tsconfig.json`): `cd tests/frontend/react && npx vitest run <file>` (`npm run test:run` runs all once; bare `npm test` starts vitest in watch mode) and `npm run typecheck` there. `src/frontend` has NO `typecheck` script — use `npx tsc --noEmit -p .` and `npx eslint <files>`. Run single files; the whole vitest suite is flaky.
- After every deploy: a browser end-to-end check is mandatory (the `smoke-tester` agent).

## Skills & Agents

| Skill/Agent | Trigger | Purpose |
|-------------|---------|---------|
| `/git-workflow` | commit, push, PR, branch | Commit format, issue numbers, PR workflow |
| `/add-integration` | neue Integration, MCP server | Add MCP server to `mcp_servers.yaml` |
| `/add-hook` | Hook, Plugin, extend | Async hook system for plugins |
| `/add-skill` | neuer Skill, procedural skill, seed skill | Add a procedural skill (`src/backend/seed_skills/*.md`); full ref `docs/SKILLS.md` |
| `/add-frontend-page` | neue Seite, add page | Page creation, routing, navigation |
| `/deploy-production` | deploy, production, rsync | Build on .159, registry, kubectl rollout (user-invoked) |
| `/debug-renfield` | debug, Fehler, broken | Troubleshooting all subsystems |
| `/verify-tests` | run the test pipeline | Test → auto-fix pipeline |
| `architecture-guide` | Architektur, how does X work | Read-only architecture Q&A (agent) |
| `satellite-deploy` | satellite deploy, provision Pi | Satellite deployment with safety rules (agent) |
| `test-runner` / `test-fixer` | run tests, fix failing tests | Test execution, diagnosis and repair (agents) |
| `smoke-tester` | post-deploy check | Browser E2E + health probes, observational (agent) |

## Rule index — where the subsystem knowledge lives

Each rule loads by itself when you read a file of that subsystem. For a question that touches no file, read the rule directly.

| Rule | Covers |
|---|---|
| `satellites.md` | voice turn + Silero VAD, BLE presence, acoustic commissioning, the Esszimmer pod, "a fleet setting has two sources", no `journalctl` on a Pi Zero |
| `satellite-trust-ota.md` | enrollment PSK (H1), signed OTA manifest (H6) — re-sign + bump on every satellite change |
| `scheduled-tasks.md` | the task engine, built-ins, failure-streak alerting, external watchdog, Paperless index health |
| `mcp-health.md` | MCP self-detection: timeouts-only health, functional probes, rate-limit signal, k8s probes |
| `kiosk.md` | `/kiosk` wall display: event-push data path, content-free payloads, health verdicts, liveness |
| `user-events.md` | `/ws/user` live refresh: Redis fan-out, content-free events |
| `agent-loop.md` | stale-error marker in the agent history |
| `internal-tools.md` | `internal.*` tools: two-step registration, injected identity, permission gates — table in `docs/INTERNAL_TOOLS.md` |
| `message-relay.md` | "sag ihm/ihr …", broadcast, the fail-closed privacy gate, `HA_CONTROL` |
| `presence-daypart.md` | day/night awareness, night LED dimming, presence history, Bluetooth scan |
| `output-routing.md` | room output devices, TTS volume + per-device sound profile |
| `ingest.md` | folder + email auto-ingest, the decoupled idempotent Paperless filing leg |
| `scanner.md` | scan jobs: event not poll, exactly-once delivery, no free text into the chat |
| `pdf-split.md` | batch-scan splitting: page count is never a signal, plan persisted before executing |
| `meetings.md` | transcription, minutes, consent, no stored voiceprints, retention |
| `chat-ui.md` | provenance/follow-up chips, palette, role hint, message search, typed artifacts, `device_action` |
| `chat-branching.md` | the conversation tree, active-path CTE, memory re-activation |
| `auth.md` | the single auth flag, provider registry, SSO code+PKCE, HttpOnly cookie + CSRF, `SECRET_KEY` |
| `circles.md` | the 5-rung tier ladder, the 4-branch SQL filter, pass `user_id`, ownership-gated writes |
| `documents-facts.md` | Schicht-A facts, generated titles, document date, document search, per-fact tier override, KB dedupe |
| `obligations.md` | deadline notifier, weekly digest, calendar sync |
| `notes-wissen.md` | notes as atoms, `[[links]]` on the KG, the unified Wissen workspace |
| `kg-memory.md` | entity resolution, merge, reconciler + person guard, graph expansion |
| `memory-extraction.md` | memory→KG bridge, subsume, spoken turns, subject tagging |
| `migrations.md` | Alembic: never edit a committed one, query the LIVE head, transaction model, real-Postgres proof |
| `deploy.md` | manifests, ConfigMaps, image ownership, PWA propagation, probes — runbook = `deploy-production` skill |
