# Renfield — Master TODO Index

Single prioritized index of every open work item, with a reference back to the source document where the original detail lives. When a topic is covered in multiple places, the **primary source** is listed first — that's the one to update when the item is actually worked.

**Tiers:**

- **P0 — Active / blocking.** Work that unblocks a commitment, gates another in-progress track, or shipped-incomplete.
- **P1 — Next substantive batch.** Ready to pick up once P0 is clear; concrete scope, no external gate.
- **P2 — Scheduled follow-ups.** Known improvements with clear scope but no forcing function yet.
- **P3 — Conditional / on signal.** Deferred pending real usage data, upstream change, or strategic green-light. Do NOT pull these forward without the trigger firing.

Last reviewed: 2026-04-23 (after PR #459 merge).

---

## P0 — Active / blocking

### Reva compatibility restoration (8 features to restore in Renfield)
Reva Enterprise Teams-bot depends on Renfield internals that were removed or changed. Blocks Reva from running on the current Renfield main.
- **Primary source:** `../reva/docs/architecture/renfield-compatibility-requirements.md`
- **Memory pointer:** `memory/project_reva_compatibility.md`
- **Items:** Semantic Router in AgentRouter · XML delimiter tags in prompts · Episodic Memory · Memory source/scope/confidence fields · Procedural memory category · `_serialize_for_prompt()` · `utils/request_context.py` · `prompt_hashes` on PromptManager
- **Also two MODERATE signature changes** (AgentService.__init__, token budget, `retrieve_for_prompt()` return format) documented in the same file §11-§13.

### KRITISCH audit findings (K1-K7)
Performance + security gaps flagged in systematic audit; each is production-exposed today.
- **Primary source:** `tasks/audit-findings-plan.md` §KRITISCH, §Priorisierte Roadmap Phase 1
- **Items:** K1-K3 three N+1 query bugs (knowledge.py, conversation_service.py) · K4 .env.example at 17% coverage · K5 missing `SecretStr` on sensitive Settings fields · K6 production Docker Secrets incomplete · K7 `EXTERNAL_URL`/`EXTERNAL_WS_URL` referenced but undefined

---

## P1 — Next substantive batch

### Reva — Phase 1 Foundation (Days 1-3)
Create the Reva private repo + plugin skeleton on top of Renfield.
- **Primary source:** `tasks/reva-plan.md` (local / untracked) §Implementation Order → Phase 1
- **Items:** private `reva` repo with submodule structure · verify hook system supports route + tool registration · ~30 lines of Renfield plugin-support changes · Dockerfile + docker-compose.yml · `reva/hooks.py` skeleton · `reva/config.py`

### Reva — Phase 2 Teams Transport (Days 4-6)
- **Primary source:** `tasks/reva-plan.md` (local / untracked) §Implementation Order → Phase 2
- **Items:** `teams_transport.py` Bot Framework adapter · `teams_auth.py` Teams↔Release user mapping · wire Teams → Renfield agent service → Teams response · Bot Framework Emulator test

### WICHTIG audit findings — performance + config hygiene
- **Primary source:** `tasks/audit-findings-plan.md` §WICHTIG, §Priorisierte Roadmap Phase 2-3
- **Items:** W1 DB connection pool tuning · W2 migrate IVFFlat → HNSW indexes · W3 bulk-insert document chunks · W4 conversation search N+1 · W5 23 hardcoded timeouts → Settings · W6 LLM options from YAML, not Python · W7-W8 circuit breaker + cache TTLs configurable · W12 `alembic.ini` hardcoded credentials · W13 config range/format validation · W14 inconsistent boolean naming

---

## P2 — Scheduled follow-ups

### Reva — Phases 3-5 (Enterprise tools, notifications, polish)
- **Primary source:** `tasks/reva-plan.md` (local / untracked) §Implementation Order → Phase 3-5
- **Phase 3 Enterprise Tools (Days 7-10):** `ldap_service.py` · `release_roles.py` 5-level lookup · register as agent tools · connect Release MCP server (38 tools, no porting)
- **Phase 4 Notifications (Days 11-12):** `notify_handler.py` webhook receiver · `subscriptions.py` DB-backed model · proactive Teams messaging · update Java plugin webhook URL
- **Phase 5 Polish & Deploy (Days 13-14):** `system_prompt.md` adapted from Roberta · tests · deploy to 192.168.99.41 · E2E verification · retire Roberta Node.js

### Paperless PR 4 inline follow-ups (documented in code, filed here)
All three have clear triggers but were out of PR 4 scope. Inline `# ...` comments in the relevant files carry full context.
- **Multi-replica `pg_try_advisory_lock`.** Current `asyncio.Lock` only covers a single process; k8s with >1 backend replica needs DB-level coordination. Source: `src/backend/services/paperless_ui_edit_sweeper.py` (top-of-file comment on `_sweep_lock`).
- **Multi-user attribution in ui_sweep rows.** Attribution seam exists (`_resolve_editor_user_id`); needs MCP `owner` exposure + Paperless↔Renfield user-mapping table to actually resolve the editor. Source: `src/backend/services/paperless_ui_edit_sweeper.py` (docstring on `_build_example_row` + `_resolve_editor_user_id`).
- **Narrow MCP `get_document_metadata` tool or `include_content=False` flag.** Eliminates the 10 KB response-truncation path entirely. Lives in the `renfield-mcp-paperless` upstream repo. Source: `src/backend/services/paperless_ui_edit_sweeper.py` comment on `_TRUNCATION_MARKER`.

### Paperless PR 4b — No-re-edit filter (superseded flag)
If ui_sweep noise shows up in real use, mark original sweep row `superseded=true` when same field edited again later.
- **Primary source:** `docs/design/paperless-llm-metadata.md` §Implementation plan → PR 4 (scope cut) + §Database schema note on `superseded` column
- **Trigger:** noise in the corpus observable in practice. Column already exists in `paperless_extraction_examples`.

### Satellite — audio pipeline improvements
- **Primary source:** `src/satellite/TECHNICAL_DEBT.md` §Future TODOs
- **High priority:** audio preprocessing (noise reduction) on backend for resource-constrained satellites — alternative: XVF3800 hardware AEC (see `docs/XVF3800_SATELLITE.md`)
- **Medium priority:** Opus audio compression (~50% bandwidth) · echo cancellation (software WebRTC APM or XVF3800)
- **Low priority:** 4-mic beamforming extension · custom wake-word training

### EMPFEHLUNG audit findings — modernization + cleanup
- **Primary source:** `tasks/audit-findings-plan.md` §EMPFEHLUNG, §Priorisierte Roadmap Phase 4-5
- **Frontend:** W9 React.lazy code-splitting for admin pages · W10 TypeScript coverage 23% → target higher via strict mode migration · W11 Prettier · E11 React Query · E12 13 hardcoded German strings · E13 ChatPage prop drilling → Context · E14 ESLint React version · E15 enable `tsconfig` strict mode
- **Backend/config:** E1-E3 speaker-loading + eager-load cleanup + FK indexes · E4-E9 remaining hardcoded values (MCP 40KB cap, backoff constants, agent history limit, agent response truncation, embedding dim, similarity threshold) · E10 frontend localhost fallbacks · E16 legacy config field removal (`ollama_model`, `piper_voice`, `plugins_*`, `spotify_*`) · E17 Redis URL parameterization · E18 Frigate MQTT defaults

### Run `/design-consultation` to formalize DESIGN.md
- **Primary source:** `TODOS.md` (root) §Run /design-consultation
- **Scope:** capture existing palette (crimson/turquoise/cream), typography (Cormorant + DM Sans), component vocabulary, animation tokens
- **Trigger:** should land before any substantive new frontend surface; low effort (~45 min)

### Write `docs/STRATEGY.md` — "why circles" strategic intent doc
- **Primary source:** `TODOS.md` (root) §Write docs/STRATEGY.md
- **Scope:** externalize the Reva-unification + federation-moonshot + household-product thesis so context survives handoff; honest framing of alternatives considered and rejected
- **Effort:** ~30-45 min writing

---

## P3 — Conditional / on signal

### Paperless PR 5 — Interactive confirm card
In-chat card with per-field controls, tag chips, storage-path tree; structured-payload callback instead of free-text.
- **Primary source:** `docs/design/paperless-llm-metadata.md` §Implementation plan → PR 5, §302-324
- **Gate:** build ONLY if cold-start data from PR 2 shows users rubber-stamp free-text confirms they can't easily edit, OR first-impression quality becomes a stated concern. Confirm is cold-start-only (first 10 uploads/user) so most users never see it.

### Paperless kNN tier (pre-LLM voter)
Embed each new upload, find k nearest Paperless docs already archived, copy dominant metadata pattern when top-k agree.
- **Primary source:** `docs/design/paperless-llm-metadata.md` §Appendix: kNN tier, deferred
- **Gate:** ALL THREE must hold — (1) v1 live 3+ months with 200+ documents · (2) Stage 1 LLM latency is the p50 UX bottleneck (> 5 s) · (3) correction rate on correspondent/document_type is low enough that kNN voting would be correct. Do not build otherwise.

### v2.5 — KG Retrieval Upgrade (gates v3 KG-as-brain)
3-5 month workstream: multi-hop traversal · edge-type ranking · community detection + summaries · inverse/transitive inference · structural query primitives.
- **Primary source:** `TODOS.md` (root) §v2.5 — KG Retrieval Upgrade
- **Also unparks:** `docs/RAG_PARITY_PLAN.md`
- **Gate:** v2 federation must ship first AND dogfooding must reveal federated-answer quality bottlenecks. If v2 federation works fine without v2.5, the unparking signal hasn't fired.
- **Minimum viable subset** (KG-1 + KG-2 + KG-4) ≈ 6-7 weeks for ~70% of benefit.

### MCPManager streaming surface
`execute_tool_streaming(name, args, on_progress) → AsyncIterator[...]` on `services/mcp_client.py:MCPManager`.
- **Primary source:** `TODOS.md` (root) §MCPManager Streaming Surface
- **Gate:** committed to for v2 federation streaming UX (per eng-review decision C-Build). Can start in parallel with v1 Lane C.
- **Effort:** ~2-3 weeks.

### Notes feature (markdown editor + bidirectional links)
Hand-written atomic notes as a 5th `atom_type` (or parallel system) with `[[link]]` syntax and graph view.
- **Primary source:** `TODOS.md` (root) §Notes Feature Design Doc
- **Gate:** circles v1 must be stable; decide before v2 whether notes are a 5th atom_type (clean) or parallel (gives notes their own model).
- **Risk:** becoming a worse Obsidian if not differentiated by voice + multi-user + circles.

### Brain Review Queue auto-archive policy (v1.5 decision)
Decide what happens to atoms in the queue the user never reviews. v1 ships with "no auto-archive"; v1.5 makes this a real decision based on usage signal.
- **Primary source:** `TODOS.md` (root) §Brain Review Queue Auto-Archive Policy
- **Gate:** 4-8 weeks of v1 usage data after Brain Review Queue ships.

---

## Source index

When updating an item, update these files (primary source first):

| Source doc | Covers |
|---|---|
| `tasks/reva-plan.md` (local / untracked) | Reva plugin architecture + 5-phase implementation plan |
| `tasks/audit-findings-plan.md` | 7 KRITISCH + 14 WICHTIG + 18 EMPFEHLUNG audit items |
| `TODOS.md` (repo root) | Strategic / longer-horizon items (v2.5 KG, MCP streaming, Notes, DESIGN.md, STRATEGY.md, Brain Queue) |
| `docs/design/paperless-llm-metadata.md` | Paperless-LLM-metadata PR roadmap (PR 5, PR 4b, kNN tier) |
| `../reva/docs/architecture/renfield-compatibility-requirements.md` | 8 Reva compatibility blockers + 3 moderate signature changes |
| `src/satellite/TECHNICAL_DEBT.md` | Satellite audio pipeline follow-ups |
| `memory/project_reva_compatibility.md` | Live pointer to the Reva compatibility doc |
