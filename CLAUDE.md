# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

Renfield is a fully offline-capable, self-hosted **digital assistant** — a personal AI hub for knowledge retrieval, tool access, and smart home control. Serves multiple household users in parallel.

**Tech Stack:** Python 3.11 + FastAPI + SQLAlchemy | React 18 + TypeScript + Vite + Tailwind CSS + PWA | Docker Compose, PostgreSQL 16, Redis 7, Ollama | Satellites: Pi Zero 2 W + ReSpeaker + OpenWakeWord

**LLM:** Local models via Ollama (multi-model: chat, intent, RAG, agent, vision, embeddings).

**Integrations:** Home Assistant, Frigate, n8n, SearXNG, Jellyfin, DLNA, Samsung TV, Paperless, Email, Calendar — all via MCP servers. (DLNA + Samsung TV run as dedicated `hostNetwork` images — `renfield-mcp-dlna` / `renfield-mcp-samsung` — not in the backend image; all other stdio servers live in the backend image.)

## KRITISCHE REGELN - IMMER BEACHTEN

**NIEMALS `git push` ohne explizite Erlaubnis des Benutzers ausfuehren!** Nach jedem Commit fragen: "Soll ich pushen?" Diese Regel gilt auch nach Session-Komprimierung. Details: `/git-workflow` Skill.

**PR-Lifecycle-Gate: Nach `/review`, VOR dem Merge, IMMER ALLE relevanten Dokumentation aktualisieren.** Kein Nachfragen nötig — das ist Pflicht-Schritt, nicht optional. Sweep statt raten: `grep -rliE "<feature-begriffe>" docs/ README.md CLAUDE.md` und jede betroffene Datei anpassen (typischerweise `CLAUDE.md`, `docs/CIRCLES.md`, `docs/SECOND_BRAIN.md`, `docs/FEATURES.md`). Doc-Update als eigener Commit in denselben PR, dann auf explizite Merge-Freigabe warten. Reihenfolge: `/review` → Docs aktualisieren → warten → merge.

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

**Alembic transaction model:** `alembic/env.py` runs with `transaction_per_migration=True` (both online and offline paths). Each migration commits independently — a mid-chain failure leaves preceding migrations applied and `alembic_version` advanced through the last success. Required so any migration may use `op.get_context().autocommit_block()` for non-transactional DDL like `CREATE INDEX CONCURRENTLY` (the assert in `autocommit_block` fires under the legacy single-outer-transaction model). Implication: when writing a migration, design it as either fully transactional or fully recoverable (e.g., `DROP INDEX IF EXISTS` before a `CONCURRENTLY` create — see `pc20260528`).

**Configuration:** `pyproject.toml` — contains ruff, pytest, and coverage config.

## Architecture

**Request Flow:** User → React Frontend → WebSocket/REST → FastAPI Backend → Intent Recognition → Action Execution → MCP/RAG → Streaming Response

**Subsystems:** Intent Recognition, Agent Loop (ReAct), MCP Integration (8+ servers), RAG/Knowledge Base, Conversation Persistence, Hook System (plugin API), Auth/RPBAC, Presence Detection, Media Follow Me, Speaker Recognition, Knowledge Graph, Paperless Audit, Audio Output Routing (+ generic **output providers** behind `OUTPUT_PROVIDERS_ENABLED` — pluggable room media/control targets via an `output_provider:` MCP stanza; new brand = config, not code; see `docs/OUTPUT_ROUTING.md` + `docs/design/output-providers.md`), Proactive Notifications (webhook + privacy-aware delivery — both WS push and TTS are presence-gated for non-public, `PROACTIVE_ENABLED`), Obligation-Deadline Notifier (`OBLIGATION_NOTIFIER_ENABLED`), Device Management, **Circles (access tiers)**

**Key config:** All via `.env` loaded by `utils/config.py` (Pydantic Settings). Full list: `docs/ENVIRONMENT_VARIABLES.md`.

For architecture questions, use the `architecture-guide` agent.

### Platform-owned internal agent tools

The agent loop sees a mix of MCP tools (`mcp.<server>.<tool>`) and `internal.*` tools. Internal tools are platform-level wrappers that bundle multi-step workflows or chain MCP calls with real server-side state. Three live on the platform core (rest live in `ha_glue`):

| Tool | Purpose | Source |
|---|---|---|
| `internal.knowledge_search` | Semantic RAG search over the user's knowledge base | `services/knowledge_tool.py` |
| `internal.list_my_memories` | Enumerate the asker's own conversation memories (preferences/facts/instructions) WITHOUT the per-turn `{memory_context}` vector threshold — backs broad self-knowledge queries ("Was weißt du über mich?") the small auto-injected snapshot can't answer. Reads only the authenticated user's own memories. | `services/memory_list_tool.py` |
| `internal.forward_attachment_to_paperless` | Forward a chat-attached file to Paperless using real server-stored bytes — prevents the LLM from handling base64 payloads it can't actually see | `services/chat_upload_tool.py` |

Dispatch for these is a special case in `services/action_executor.py` that injects dependencies the generic `intent.startswith("internal.")` hook path cannot provide (`mcp_manager`, `session_id`, and the authenticated `user_id` for `list_my_memories`).

### Agent stale-error marker

Failed tool turns are persisted with `action_success: False` in message metadata. The `conv_context` builder in `services/agent_service.py` prepends `[VORHERIGE_FEHLGESCHLAGENE_AKTION]` to those assistant messages when re-injecting history into the next agent turn. The `conv_context_template` in `prompts/agent.yaml` carries a hint telling the LLM to treat marker lines as historical, not as current state — so a repeated user request retries the tool instead of echoing the old error.

### Pluggable auth provider registry (ebongard/renfield#591)

`/auth/login` no longer hard-codes "an `authenticate` hook then bcrypt". It
delegates to `auth/login_flow.py::resolve_login`, which: (1) still honors the
legacy `authenticate` hook first (a plugin returning a `User` is authoritative
— unchanged backward-compat seam), then (2) runs the **provider registry**
credential walk (`auth/registry.py`): providers ordered by ascending
`priority`, multi-active, per-provider `enabled` gate, **first non-None wins**.
A provider that raises or exceeds `auth_provider_timeout_seconds` is
**skipped fail-open** — WARNING log + `auth_provider_unreachable_total{provider_id}`
counter + continue the walk. (3) On a `ProviderResult`, the **single
`post_authenticate` hook** fires exactly once *before* the JWT is minted.

The cross-repo contract is `auth/provider_contract.py` (`ProviderResult` frozen
dataclass + `AuthProvider` Protocol + `PROVIDER_RESULT_CONTRACT_VERSION`).
Renfield owns authn; the Reva consumer (a `post_authenticate` handler) owns
identity resolution and returns the renfield user id. **Standalone fallback:**
0 handlers registered → JWT minted from the `db` provider's subject (legacy
behavior, keeps the renfield test suite green); ≥1 handler registered but none
resolve → login denied (no half-bound token). JWT `sub` unchanged; the cosmetic
`username` claim now carries `display_name` (no consumer reads it).

Built-ins (`auth/providers/`): `db` (priority 100, always on, wraps bcrypt),
`ldap` (priority 50, authn-only — no local-user create; that is the
identity consumer's job; config-gated), `google`/`github`/`apple` (redirect
providers, **`enabled=False` by default**, enabling is config-only). The
group→role **authz seam is defined in docstrings only**, not implemented this
delivery (`extras["ldap_member_of"]` is carried but unused). New config:
`ldap_auth_*`, `oauth_{google,github,apple}_*`, `auth_provider_timeout_seconds`.

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

Key services: `services/circle_resolver.py` (PolicyEvaluator + cache), `services/atom_service.py` (upsert + tier cascade), `services/polymorphic_atom_store.py` (cross-source RRF), `services/document_fact_retrieval.py` (Schicht A fact reads: keyword FTS + identifier-ILIKE + obligations, circle-filtered), `services/kb_shares_service.py` (KB-level share → per-chunk grant explosion), `services/circle_sql.py` (shared filter clause builder; the document owner-branch has an atom-owner fallback so null-KB / global-RAG docs reach their owner).

Key routes: `/api/atoms` (unified search + edit; `document_fact` is a fused RRF source so Schicht A facts surface in `/brain` with a green "Fakt" badge), `/api/atoms/documents/{id}/facts` (per-doc facts, 404/403-gated on the parent document), `/api/atoms/obligations` (bills + Behörde deadlines, soonest Frist first; `due_before` + `limit` + `offset` paging for the agenda's "Mehr laden"; each row carries the asker's per-user `confirmed` Bestätigt state), `/api/atoms/obligations/{id}/confirm` (POST/DELETE — per-user Bestätigen/Wieder öffnen, circle-gated 404, server home for the former localStorage state), `/api/atoms/obligations/export.ics` (circle-filtered iCalendar of dated obligations — one all-day VEVENT each, RFC-5545-escaped, browser-native download from the agenda), `/api/atoms/obligations/calendar-pref` (GET/PUT — per-user opt-in calendar for the obligation→calendar auto-push; clearing tears down the user's synced events), `/api/config/features` (frontend-visible feature-flag allowlist — `schicht_a_extraction_enabled` + `wissen_workspace_enabled`; the one intentional settings→browser seam), `/api/circles/me/*` (settings, members, review queue), `/api/knowledge-graph/circle-tiers` (localized ladder labels), `/api/knowledge-graph/entities/{id}/circle-tier` (tier patch with cascade to incident relations), `/api/wissensbasis/{graph,focus,search}` (native backend for the 3D Wissensgraph tab — corpus connected-component clusters / entity hop1+hop2 neighborhood / name-substring search over `kg_entities`+`kg_relations`, all circle-filtered via `services/kg_graph_service.py`; Reva's richer `/trace`+`/me/mix` stay 404 in standalone Renfield, which is what `useWissensbasisAvailable` keys off to hide the Reva-only side panels).

Frontend pages: `/brain` (search), `/brain/review` (owner review queue), `/brain/fristen` (obligations agenda — deadline inbox grouped by urgency Überfällig/Diese Woche/Später, `⚑ rechtlich` on legal_gate facts, **server-backed Bestätigen** with undo toast via the obligation ledger), `/settings/circles` (members). The `/knowledge` document cards carry an inline **Fakten** panel (`FaktenPanel`, lazy-fetch on expand) and bidirectionally deep-link with the agenda (`?doc={id}#fakten` ↔ `#frist-{id}`). Shared `TierBadge` + `TierPicker` + `FactProvenance` (✓ deterministic / ~ advisory) + `ObligationRow` components use the `.tier-badge-{0..4}` / `.fact-group` / `.legal-flag` utilities from `index.css`.

**Per-fact tier override.** A `document_fact` can carry a tier independent of its parent document (e.g. a public issuer on an otherwise-private document). `document_facts.tier_overridden`: a direct per-fact tier PATCH sets it True, and `AtomService.update_tier`'s kb_document cascade only re-tiers facts `WHERE NOT tier_overridden` — the override is sticky in **both** directions (a public issuer stays public even after the doc is privatized). `AtomService.reset_fact_tier` + `POST /api/atoms/documents/facts/{id}/reset-tier` (owner-only) clear it back to the document tier. The Wissen detail drawer's TierPicker sets the override and surfaces a reset action; FaktenPanel shows a read-only override marker. Limitation: re-ingest/re-OCR recreates facts fresh, so overrides don't survive a re-extraction (a re-extracted fact reverts to its doc tier; carry-over is a P2, TODOS.md).

**Obligation-deadline notifier (`OBLIGATION_NOTIFIER_ENABLED`, opt-in/dark; also requires `PROACTIVE_ENABLED`).** `services/obligation_deadline_notifier.py` — one daily idempotent, **owner-targeted** scan (`_schedule_obligation_deadline_notifier`, `run_at_boot`, per-user advisory lock reusing the KG-reconciler helper) over dated `document_facts` obligations. `current_milestone(days_until)` returns the SINGLE current lead-time bucket (`14d`/`7d`/`3d`/`1d`/`due`/`overdue`) so first-enable can't back-fire every crossed milestone; each fires once via `NotificationService.process_webhook(target_user_id, privacy="personal")` and is recorded in the `obligation_acknowledgements` ledger (`(document_fact_id, user_id, milestone)` UNIQUE) so a pod restart never re-fires (the missed-deadline safety property). Legal-gate kinds are notified but flagged human-gated (message → `/brain/review`), never auto-acted. The same ledger's `"confirmed"` milestone is the per-user Bestätigt store (a confirmed ack also suppresses the owner's further milestones). Design per the cross-model learning `schicht-a-obligations-source-of-truth` (obligations ARE the scheduling source of truth — no `Reminder` rows, no reuse of the chat-reminder loop). Scan window `[today − OBLIGATION_NOTIFIER_OVERDUE_GRACE_DAYS, today + 14d]`. **Delivery privacy:** `ha_deliver_notification` presence-gates BOTH the WS push and TTS for non-public notifications (fail-closed) — a `privacy="personal"` reminder never fans out to all household devices. `NotificationService._compute_dedup_key` includes `target_user_id` so two members' identical per-user notifications aren't cross-deduped.

**Weekly obligation digest (`OBLIGATION_DIGEST_ENABLED`, opt-in/dark; also requires `PROACTIVE_ENABLED`)** — the safety floor *under* the per-milestone notifier. `services/obligation_digest.py` (`_schedule_obligation_digest`, weekly, run_at_boot, per-user advisory lock ns `0x4F44`): once per ISO week sends each owner ONE summary of every OPEN obligation with **no lower date bound**, so a late-extracted / very-overdue deadline the notifier's grace window missed still surfaces (it cannot catch *never-extracted* — that stays observable upstream). Deduped by a `(user, period_key)` row in `obligation_digest_log` (dedicated table, not the TTL-reaped `notifications` row); the ISO week is in the title so two weeks' digests stay content-distinct.

**Obligation → calendar auto-push (`OBLIGATION_CALENDAR_SYNC_ENABLED`, opt-in/dark; needs the Calendar MCP).** `services/obligation_calendar_sync.py` (`_schedule_obligation_calendar_sync`, daily, run_at_boot, per-user advisory lock ns `0x4F43`) is a per-user stateless reconciler: it diffs each opted-in user's open obligations against the `obligation_calendar_events` ledger (fact→event_id) and create/update/deletes events via the Calendar MCP (`mcp.calendar.{create,update,delete}_event`, owner-scoped — the MCP enforces per-calendar write access by `user_id`). Per-user opt-in via `obligation_calendar_pref` (no pref → no sync; `GET/PUT /api/atoms/obligations/calendar-pref`, clearing tears the user's events down first). Ledger FK is `ON DELETE SET NULL` so a purged fact orphans the row (event_id kept) for the next pass to delete. Idempotent + restart-safe + op-capped; not-found-delete is treated as done; MCP failures retry. Events are timed at `obligation_calendar_event_hour` (all-day unsupported by the MCP). Known: at-least-once duplicate window on a crash between create + ledger-commit (no MCP idempotency key; P2 in TODOS.md).

**Unified Wissen workspace (`wissen_workspace_enabled`, off by default).** When the flag is on, the six corpus surfaces above collapse into one `/wissen` workspace (`pages/wissen/WissenLayout.tsx`): a persistent left lens-rail (Übersicht · Dokumente · Graph · Erinnerungen · Fristen · Prüfen — `components/wissen/LensRail.tsx`, gated per-lens by the same permission/feature metadata in `pages/wissen/lenses.ts`), a persistent **lens-scoped omnisearch** (`WissenSearchBar`; `?scope=lens|everything` — on Documents/Graph the query drives that lens's *own* inline search, else a cross-corpus RRF overlay), and a **universal detail drawer** (`WissenDetailDrawer`, opened from any result, per-type content + two-id-space tier edit: atom UUID via `usePatchAtomTier`, `kg_node` via the KG int-id `useUpdateKgEntityTier`). The old routes (`/knowledge`, `/brain`, `/brain/review`, `/brain/fristen`, `/memory`, `/knowledge-graph`) redirect into `/wissen/*` (search + hash preserved) when on; off = the legacy flat nav, byte-identical. Skills (`/brain/skills`) + Federation Audit (`/brain/audit`) stay standalone. The shell persists across lens switches (`Layout.tsx` keys `/wissen/*` on a stable content key). To support per-entity Graph results + drawer, `PolymorphicAtomStore` now emits per-entity `kg_node` + per-relation `kg_edge` atoms (via `KGRetrieval.get_relevant_atoms`) instead of the old aggregated blob; the agent's string KG context (`get_relevant_context`) is unchanged.

**Behavioral change vs pre-circles:** `ConversationMemoryService.retrieve()` now respects circle reach — tier-2 household peers see each other's household-tier memories. Previously `user_id == asker_id` filtered strictly. Flag in release notes.

For memory-retrieval callers: pass `user_id=asker_id`. For RAG search: pass `user_id=asker_id` in every `rag.search()` call — `None` reduces to public-tier-only in auth-enabled mode.

### Structured Memory (KG canonicalization + subject attribution)

Lifts personal memory from flat text onto the typed KG substrate.

**Schema (additive on the circles tables).** `kg_entities`: `canonical_id` self-FK (NULL = canonical/live; non-NULL = merge tombstone → survivor, mirrors `procedural_skills.merged_into_id`), `surface_forms` JSONB (absorbed aliases, GIN `jsonb_path_ops`), `entity_types` JSONB (multi-type superset; scalar `entity_type` stays the closed-enum primary), `external_id` (column only). `kg_relations`: `stated_by_user_id` (who asserted the fact, ≠ owner) + `source_message_id`. `conversation_memories`: `subject_entity_id` + `subject_name` (WHO the fact is about). Migrations `pc20260604_struct_mem` + `pc20260604b_kgmp`.

**Entity resolution cascade** (`KnowledgeGraphService.resolve_entity`): exact name → surface-form (jsonb `@>`) → embedding (SAME-TIER only + high threshold, `::halfvec`, name+description) → create new. It never folds across tiers or on a weak signal inline; those become reconciler proposals. **PERSON entities skip the embedding-match step entirely** (gate keyed on the multi-type `seed_types`, so a person carried as a secondary type is covered too): people are identified by name (exact + surface-form), and a generic meta-description ("Vollständiger Name einer Person") would otherwise turn a row into a generic-person *centroid* that any bare name folds into — the 127-mention magnet-hub bug. Non-person types keep embedding-match (salvages OCR/typo variants like Bnn→Bonn). The embedding is still computed + stored for persons (backs retrieval + reconciler dedup); only the inline match is suppressed. Defense in depth: `resolve_entity` strips generic descriptions (`is_generic_person_description`, **whole-string** match) from person rows before embed/store so new rows never re-create the centroid, and `services/kg_demagnetize.py` (+ `bin/demagnetize_person_entities.py`, `--dry-run`/`--apply` with a fail-closed pre-mutation audit dump) repairs existing magnet rows by NULLing the generic description and re-embedding name-only. The extraction prompt (`prompts/knowledge_graph.yaml`, all 4 variants) requires an entity-specific description or empty, never a type-meta-description. **Conflation tripwire** (`services/kg_conflation_monitor.py`, `KG_CONFLATION_MONITOR_ENABLED`, opt-in, read-only): a periodic per-user halfvec self-join logs + gauges (`renfield_kg_conflation_candidates`) **distinct-name, same-type, same-tier NON-person** pairs embedding ≥ threshold — a forming magnet in a type where resolve still embedding-matches. **Persons are excluded** (primary OR multi-type): their names inherently cluster ≥ threshold name-only (measured Jutta~Anna 0.894) and resolve skips embedding-match for them anyway, so a close person pair can't fold — flagging it would be permanent noise. Expected 0; never mutates; on-demand via `bin/scan_kg_conflation.py`. **`merge_entities(loser, winner)`** ports the skill-curator merge (FOR UPDATE + shared `services/merge_guard.is_already_merged`) and adds entity-specific work: reparent `kg_relations` FKs, re-dedup, recompute `circle_tier=LEAST(subject,object)` + atom policy, follow `conversation_memories.subject_entity_id`, tombstone the loser. **Invariant: a merge never raises visibility** (survivor tier = MIN).

**Reconciler** (`services/kg_reconciler_service.py`, `KG_RECONCILER_ENABLED`, opt-in): periodic per-user halfvec self-join → same-tier high-confidence dupes auto-merge; cross-tier / gray-zone become `kg_merge_proposals` for owner review (never silently merged). **Person-guard** (makes it safe to enable, mirrors resolve's person embedding-skip): a person-involving pair (either side person-typed, primary OR `entity_types` contains person) whose names are UNRELATED is dropped entirely — no merge, no proposal — because distinct person names embed ≥ the candidate threshold by themselves (`_names_related` = equal or whitespace-token-subset, e.g. "Alice" ⊆ "Alice B.", "Jutta" ⊆ "Jutta van den Bongard"). The auto-merge gate re-requires name-relatedness for person pairs (defense in depth behind the find-time drop), so a detection miss can't silently merge two distinct people. Each pass is serialized per-user by a non-blocking advisory lock (`_RECONCILER_LOCK_NS`; an overlapping run is a no-op) and first re-embeds up to `KG_RECONCILER_EMBED_BACKFILL_PER_RUN` null-embedding entities (else they stay invisible to the self-join). Approving a proposal whose counterpart a concurrent approve already merged closes it as `superseded`, not `approved`. Scheduler `_schedule_kg_reconciler` (run_at_boot). Routes (all `KG_VIEW`, scoped to the caller's own graph + per-proposal ownership 404): `/api/knowledge-graph/merge-proposals` (GET), `…/{id}/approve` (optional `winner_id` survivor override), `…/{id}/reject`, `/reconciler/run`. Frontend: `MergeProposalsSection` + `MergeProposalCard` at the top of `/brain/review` (comparison + survivor toggle + cross-tier warning + 5s undo toast).

**Memory→KG Bridge (Phase 3, `MEMORY_KG_BRIDGE_ENABLED`, opt-in/dark by default).** Closes "Was weiß ich über X" determinism by linking flat memories to canonical entities. **3a** made `resolve_entity` bridge-safe with two additive, backward-compatible params: `create_tier` (replaces the hardcoded tier-0 for the create path + same-tier embedding search; the bridge passes the source memory's `circle_tier`) and `match_entity_type` (scopes exact-name/surface-form/embedding lookups to the primary `entity_type` so a "Bella" person-fact never links a place); plus a reconciler **same-name gate** (a pair sharing a normalized name with empty/identical descriptions never auto-merges → review, closing the conflation feedback). **3b** bridges: `ConversationMemoryService._bridge_subject_entity` resolves `fact`/`preference` memory subjects to `subject_entity_id` in the **background** extraction path (never the sync turn); `services/memory_bridge_backfill.py` (+ `bin/backfill_subject_entity_ids.py`, `--dry-run`/`--commit`) backfills existing rows — resolve-or-create at `memory.tier`, per-row atomic create+link, idempotent. **3c** retrieval: `memory_retrieval.retrieve` resolves query-named entities (exact word-token + surface-form, no LLM) and **unions** their `subject_entity_id` memories into the embedding hits (similarity floor, own `MEMORY_RETRIEVAL_SUBJECT_UNION_LIMIT`, `canonical_id` tombstone-chase, deduped) — **through the same `circle_sql` filter** as the embedding branch (no second unfiltered path). `GET /api/memory/by-subject/{entity_id}` (circle-filtered) backs the `/wissen` entity drawer's "Erinnerungen über diesen Knoten"; `subject_name`/`subject_entity_id` ride on `MemoryResponse`. Off = retrieval/extraction byte-identical. **Subsume (`MEMORY_SUBSUME_TO_KG`, opt-in, aggressive):** when on, decomposable `fact` memories with a subject are not stored flat at all (they live in the KG); preferences/instructions/context stay flat. Recall-loss risk if a fact's object isn't a named entity — validate KG fact-capture before enabling (see `TODOS.md`). **Phase 4 — graph_expansion (multi-hop retrieval, `GRAPH_EXPANSION_ENABLED`, opt-in/dark).** `services/graph_expansion.py::expand_fused` runs **post-RRF** in `PolymorphicAtomStore.query`: takes the fused `AtomMatch` list, finds the top `kg_node` pivots, walks `kg_relations` 1-`GRAPH_EXPANSION_MAX_HOPS` hops (**level-synchronous BFS** → correct min-hop distance; `kg_entities_circles_filter` per hop; per-hop frontier cap; **leak-safe `kg_edge`s** only when both endpoints are accessible; decay = pivot/(1+hop); cap `GRAPH_EXPANSION_MAX_EXPANDED`), and appends provenance-marked (`payload.expanded`+`hop`) neighbour atoms, re-sorted, capped. Single seam (no double-work, decay survives) — the rebuild after the per-module MVP was re-deferred by `/plan-eng-review` (the MVP is parked on `feature/structured-memory-phase4-subsume`). Off = `query` byte-identical. Follow-up (`TODOS.md`): route the agent string path `get_relevant_context` onto the fused path so `internal.knowledge_search` benefits too. (The pre-existing unfiltered `name_map` endpoint-name leak in `get_relevant_atoms`/`get_relevant_context` was fixed 2026-06-06 — both now route endpoint-name resolution through the shared `KGRetrieval._resolve_entity_names` → `kg_entities_circles_filter`, "?" on miss, mirroring `kg_graph_service.focus`.)

**The conflation fix (D9):** memory extraction now binds each fact to a `subject_name`, retrieval carries it, and the injected context is subject-tagged (`- [FACT · <name>] …`) so the LLM cannot conflate facts about different people. Extraction also emits multi-type entities + tastes-as-relations and records `stated_by`. KG-extraction eval: `bin/run_kg_extraction_eval.py` + `tests/eval/kg_extraction_eval.yaml`.

## Testing

Tests in `tests/` at project root. Backend: 3,400+ tests.

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
