# TODOS — Renfield Master Index

Single prioritized index of every open work item, with a reference back to the source document where the original detail lives. When a topic is covered in multiple places, the **primary source** is listed first — that's the one to update when the item is actually worked.

**Tiers:**

- **P0 — Active / blocking.** Work that unblocks a commitment, gates another in-progress track, or shipped-incomplete.
- **P1 — Next substantive batch.** Ready to pick up once P0 is clear; concrete scope, no external gate.
- **P2 — Scheduled follow-ups.** Known improvements with clear scope but no forcing function yet.
- **P3 — Conditional / on signal.** Deferred pending real usage data, upstream change, or strategic green-light. Do NOT pull these forward without the trigger firing.

Long-form strategic items (formerly a separate `TODOS.md`) carry a `**WHAT/WHY/PROS/CONS/CONTEXT/DEPENDS ON**` block when the rationale is non-trivial.

Last reviewed: 2026-04-27 (WICHTIG sweep CLOSED — 14 of 14 items resolved: W1/W4/W7/W8/W9/W11/W12/W14 verified already done in previous work; W2/W3/W5/W6/W13 closed via #482-#485 on 2026-04-26; W10 closed via #487 on 2026-04-27 — full frontend TypeScript migration).

---

## P0 — Active / blocking

_(no active blockers — all prior P0 items resolved and merged)_

---

## P1 — Next substantive batch

_(no P1 items — WICHTIG sweep complete. W10 closed via #487 on 2026-04-27. Audit-findings-plan.md still lists pre-resolution items in body text; the per-item lines and Phase 4-5 checklists have been updated to reflect closure.)_

---

## P2 — Scheduled follow-ups

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
- **Frontend:** W9 React.lazy code-splitting for admin pages · W11 Prettier · E11 React Query · E12 13 hardcoded German strings · E13 ChatPage prop drilling → Context · E14 ESLint React version · E15 enable `tsconfig` strict mode (W10 closed via #487 on 2026-04-27)
- **Backend/config:** E1-E3 speaker-loading + eager-load cleanup + FK indexes · E4-E9 remaining hardcoded values (MCP 40KB cap, backoff constants, agent history limit, agent response truncation, embedding dim, similarity threshold) · E10 frontend localhost fallbacks · E16 legacy config field removal (`ollama_model`, `piper_voice`, `plugins_*`, `spotify_*`) · E17 Redis URL parameterization · E18 Frigate MQTT defaults

### Run `/design-consultation` to formalize DESIGN.md (BEFORE next major frontend surface)

**WHAT:** Run the `/design-consultation` skill to formalize Renfield's existing implicit design system into a DESIGN.md file. Captures the palette (crimson primary + turquoise accent + cream neutral), typography (Cormorant Variable display + DM Sans Variable body), component vocabulary (cards, inputs, buttons, animations), and design philosophy.

**WHY:** The circles v1 design review found that Renfield has a sophisticated visual system in `src/frontend/src/index.css` that's doing the work of a design system without ever being named. Adding new pages + a tier visual language + dimension-agnostic UI is much easier (and more consistent) when those rules are explicit before implementation begins.

**PROS:**
- Prevents design drift across new pages
- Makes design decisions debuggable ("does this fit DESIGN.md?")
- Creates shareable artifact for future contributors
- Catches inconsistencies in the existing system that have crept in over time

**CONS:**
- 30-45 minutes of conversation (small cost for the leverage gained)
- Documentation rot risk if not maintained alongside design changes (mitigated by /design-review skill referring to DESIGN.md)

**CONTEXT:**
- Existing palette in `src/frontend/src/index.css`: `--color-primary-{50..900}` (crimson family centered on #e63e54), `--color-accent-{50..900}` (turquoise centered on #00e4b8), `--color-cream` (#f0e6d3)
- Existing typography: `--font-display` (Cormorant Variable serif), `--font-sans` (DM Sans Variable sans)
- Animation tokens already defined: `--animate-typing-dot`, `--animate-fade-slide-in`, `--animate-slide-in-right`, etc.
- 19 existing pages provide pattern reference; KnowledgePage / RolesPage / MemoryPage are the closest analogs for new circles surfaces

**DEPENDS ON:**
- Should land BEFORE the next substantive frontend surface
- Independent of all back-end work

**SOURCE:** `~/.gstack/projects/ebongard-renfield/evdb-main-design-20260419-190713-second-brain-circles.md` design-review pass

### Write `docs/STRATEGY.md` — North-Star "WHY circles" doc

**WHAT:** A strategic intent document that captures WHY the Second Brain Circles plan exists, distinct from the HOW captured in the design doc and DESIGN.md. Documents the Reva unification thesis, the federation moonshot rationale, the household-product positioning, and the strategic context that motivated the 9-12 month foundation investment over alternative paths (small household features, Reva commercial pursuit, public Renfield launch, 6-week MVP).

**WHY:** The /plan-ceo-review surfaced via outside voice that the strategic premise is currently legible only to the user. The design doc has architecture but not intent. The DESIGN.md will have visual system but not strategic context. If the user takes a sabbatical, hands the project off, or comes back in 18 months after Reva pulls focus, the next person inherits ambitious infrastructure with no documented WHY. STRATEGY.md fills that gap.

**PROS:**
- Strategic context survives session compaction + project handoff + memory drift
- Future eng/CEO reviews of v2/v2.5/v3 work have a north-star to evaluate against ("does this still serve the strategic intent?")
- The Reva unification claim becomes inspectable instead of implicit
- Forces articulation of the 5-year ideal (per Section 10 dream-state delta)
- 30-min effort for arguably the highest-leverage doc in the project

**CONS:**
- 30-45 min of writing
- Risks becoming "vision theater" if not written honestly (the outside voice's #2 critique — "Reva unification is rationalization, not strategy")

**CONTEXT:**
- Per CEO review HOLD SCOPE + 1C decision: the maximalist plan stands BECAUSE the user has strategic context the outside voice doesn't. STRATEGY.md externalizes that context.
- Honest framing should include: which alternative strategic moves were considered and rejected (6-week MVP, public launch, Reva commercial-first, small household features) AND the user's reasons for choosing the maximalist circles path over them
- Should reference: design doc, DESIGN.md, Reva memory note, feature-ideen.md (the path-not-taken alternatives)
- Should be HONEST about the field-of-dreams risk (federation has no second peer yet) and what would invalidate the bet

**DEPENDS ON:**
- Pre-implementation gate conversations (Reva + partner) ideally happen FIRST so STRATEGY.md can incorporate their findings
- Independent of all v1 implementation work

**SOURCE:** /plan-ceo-review session 2026-04-19, Section 10 + outside voice cross-model tension 1

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

**WHAT:** A focused 3-5 month workstream upgrading Renfield's KG retrieval from "flat 1-hop entity lookup" to proper graph-aware retrieval (multi-hop traversal, edge-type ranking, optional hierarchical summaries, inverse/transitive inference, structural query primitives).

**WHY:** v3 KG-as-brain migration is currently "open-ended" because today's KG retrieval (`services/knowledge_graph_service.py:867-1012`) is significantly weaker than chunk-level RAG. v2.5 closes that gap so v3 becomes a clean swap. v2.5 also unparks the broader retrieval-quality work in `docs/RAG_PARITY_PLAN.md` (which was parked pending real usage signal).

**PROS:**
- v3 timeline becomes estimable (~6 months) instead of indefinite
- Closes the published gap with LightRAG/GraphRAG/RAG-Anything
- Synergistic with parked RAG_PARITY_PLAN items (query decomposition, citation bbox)
- Each sub-item is independently shippable

**CONS:**
- 3-5 months of work that doesn't add user-facing features directly (improves answer quality on graph-shaped queries)
- Hierarchical summaries (KG-3) is the highest-ROI but highest-implementation-risk item
- Requires v2 federation usage signal to justify priority — premature without that signal

**CONTEXT:**
- 5 sub-items, ROI-ordered:
  - **KG-1 multi-hop traversal** (~3 weeks): 1→N hops with depth budget + relevance decay. Recursive CTE in PostgreSQL or Python graph-walk.
  - **KG-2 edge-type-aware ranking** (~2 weeks): weight relations by predicate type via curated YAML.
  - **KG-3 community detection + summaries** (~6-8 weeks): Leiden clustering + per-community LLM summaries; query routes to relevant communities (GraphRAG state-of-the-art).
  - **KG-4 inverse/transitive inference** (~2 weeks): rule pack for inverse predicates; materialized derived-relations table refreshed nightly.
  - **KG-5 structural query primitives** (~3-4 weeks): Cypher subset (find_path, expand_neighbors, find_subgraph) exposed as agent tools.
- Minimum-viable subset (KG-1 + KG-2 + KG-4) ≈ 6-7 weeks for ~70% of practical benefit.
- Triggered by v2 dogfooding revealing federated-answer quality bottlenecks. If v2 federation works fine without v2.5, the unparking signal hasn't fired.
- Also unparks `docs/RAG_PARITY_PLAN.md`. Cross-reference that doc when v2.5 starts; flip its Status from `PARKED` to `MERGED INTO v2.5 of second-brain-circles`.

**DEPENDS ON:**
- v2 federation must ship first (provides the demanding-retrieval workload that justifies the upgrade)
- Refactor-first work in v1 must be complete (KG-2.5 references `kg_retrieval.py`, not the megaservice)

**SOURCE:** `~/.gstack/projects/ebongard-renfield/evdb-main-design-20260419-190713-second-brain-circles.md` v2.5 section

### MCPManager Streaming Surface

**WHAT:** Add `execute_tool_streaming(name, args, on_progress) -> AsyncIterator[ProgressChunk | FinalResult]` to `services/mcp_client.py:MCPManager`. Existing `execute_tool` returns one dict and exits — there's no streaming progress callback API.

**WHY:** Required for v2 federation streaming UX (per design doc decision C-Build). Also generally useful for any long-running MCP tool: streaming TTS, long n8n workflow execution, video generation, large file uploads, pipeline observability.

**PROS:**
- Unblocks v2 federation streaming progress chunks ("waking up... retrieving... synthesizing...")
- Generic capability — every future long-running MCP tool wants this
- Additive to existing API (old `execute_tool` callers keep working)

**CONS:**
- ~2-3 weeks of MCP infra work
- Requires every MCP transport (stdio, streamable_http) to support the streaming contract
- Frontend + agent loop need to consume streamed results — non-trivial wiring

**CONTEXT:**
- Current `execute_tool` at `services/mcp_client.py:1038`: returns `{"success": bool, "message": str, "data": Any}` synchronously
- The MCP SDK's `ClientSession` does support streaming responses — the limitation is on the Renfield wrapper, not the protocol
- Choice C-Build in the eng-review committed to this work for v2 federation

**DEPENDS ON:**
- Independent of v1 work; can start in parallel with v1 Lane C (frontend)
- v2 federation work will consume this API once shipped

**SOURCE:** `~/.gstack/projects/ebongard-renfield/evdb-main-design-20260419-190713-second-brain-circles.md` v2 section + eng-review C-Build decision

### Notes Feature Design Doc (markdown editor + bidirectional links)

**WHAT:** A separate office-hours / design session for hand-written atomic notes — markdown editor, bidirectional `[[link]]` syntax, graph view, optional outliner mode. Was descoped from circles v1 because notes-as-product is its own surface (not just an access-control concern).

**WHY:** The "second brain like Obsidian" framing in the original feature ask implies notes. v1 ships circles-on-existing-atoms only (chunks, KG facts, memories). Without notes, Renfield's second brain only grows from passive capture and document upload — there's no "I want to write something down right now" surface.

**PROS:**
- Completes the second-brain UX story (capture + write + edit + link)
- Natural integration with circles framework: notes become a 5th `atom_type`
- Bidirectional links are a different retrieval primitive (graph-of-notes) that could feed v2.5 KG-5 structural queries

**CONS:**
- Substantial product surface (markdown editor, link rendering, graph view)
- Risk of becoming a worse Obsidian if not differentiated by Renfield's voice + multi-user + circles unique strengths
- Adds a 5th atom_type → expands `AtomPayload*` TypedDict surface (Open Q 7 in design doc)

**CONTEXT:**
- Office-hours conversation pushed back hard against shipping notes in v1 — too distinct from the access-control feature, would smuggle a whole product into the circles design
- Notes-on-atoms vs notes-alongside-atoms is the first design fork (does a note become an atom that wears a circle, or does a note exist parallel to atoms?)
- Should sit on top of circles v1 (notes inherit circle_tier on creation; tier-edit affordance like other entity views)

**DEPENDS ON:**
- Circles v1 stable (so notes can lean on the atom + tier infrastructure)
- Decide before v2 whether notes are a 5th atom_type (clean) or a parallel system referencing atoms (gives notes their own model)

**SOURCE:** `~/.gstack/projects/ebongard-renfield/evdb-main-design-20260419-190713-second-brain-circles.md` Premise 2 + Open Q 1 + Open Q 12

### Re-enable `itsm` MCP on roberta when USU customer service is back
During the 2026-04-25 Reva bump test deploy on roberta, the `itsm` MCP server (`http://usu-mcp.reva.treehouse.local/mcp`) was throwing `Connection refused` — USU customer-side service was down. Reva's `/api/health` returns 503 if any MCP server is disconnected, blocking pod readiness. Workaround was to flip `enabled: true → false` for the itsm block in roberta's `reva-mcp-config` ConfigMap.

PRD is unaffected (separate cluster, separate ConfigMap, `usu-mcp` actually running there). Roberta-only loose end.

- **Reverse with:** `ssh evdb@192.168.99.41 -- kubectl edit configmap reva-mcp-config -n reva` and flip `enabled: false → true` in the itsm block, then `kubectl rollout restart deployment/reva -n reva`.
- **Gate:** USU customer service back up + reachable from roberta cluster (verify with `kubectl exec -n reva deployment/reva -c reva -- python3 -c 'import urllib.request,socket; socket.setdefaulttimeout(5); urllib.request.urlopen("http://usu-mcp.reva.treehouse.local/mcp")'`).

### Brain Review Queue Auto-Archive Policy (v1.5 decision)

**WHAT:** Decide what happens to atoms in the Brain Review Queue that the user never reviews. v1 ships with "no auto-archive, queue may grow." v1.5 should make this a real decision based on actual usage signal.

**WHY:** The queue surface needs to stay useful. If users review atoms within ~3 days reliably, anything older is stale and should auto-archive. If users review on a weekly cadence, 7+ days is fine. The right answer depends on real behavior, which we don't have yet.

**PROS (deferring to v1.5):**
- Avoids guessing the cadence
- Real usage data drives the decision
- v1 ships sooner without this debate

**CONS:**
- Risk: queue grows unbounded for users who never review (engagement drop, perceived feature failure)
- v1 users may have a worse first impression if they let atoms accumulate

**CONTEXT:**
- v1 Brain Review Queue spec: shows atoms ≤7 days old, owner-only, paginated
- Choices considered: auto-archive at 30d (reasonable but arbitrary), no auto-archive ever (explicit but risky), tied to user behavior signal (best, requires data)
- Likely v1.5 outcome: auto-archive at 14d for atoms unreviewed, with a "queue health" indicator showing how far behind the user is

**DEPENDS ON:**
- 4-8 weeks of v1 usage signal (after Brain Review Queue ships in Phase 2 of v1)

**SOURCE:** `~/.gstack/projects/ebongard-renfield/evdb-main-design-20260419-190713-second-brain-circles.md` design-review Pass 7

---

## Source index

When updating an item, update these files (primary source first):

| Source doc | Covers |
|---|---|
| `tasks/audit-findings-plan.md` | 14 WICHTIG + 18 EMPFEHLUNG audit items (KRITISCH K1-K7 done as #464) |
| `docs/design/paperless-llm-metadata.md` | Paperless-LLM-metadata PR roadmap (PR 5, PR 4b, kNN tier) |
| `../reva/docs/architecture/renfield-compatibility-requirements.md` | 8 Reva compatibility blockers (ALL VERIFIED on PRD 2026-04-26 via Reva PR #177) |
| `../reva/docs/operations/upgrade-guide.md` §7 | Existing-DB upgrade dance for Reva submodule bumps (added during 2026-04-26 bump) |
| `src/satellite/TECHNICAL_DEBT.md` | Satellite audio pipeline follow-ups |
| `memory/project_reva_compatibility.md` | Memory pointer to the Reva compatibility status (now: verified) |
| `~/.gstack/projects/ebongard-renfield/evdb-main-*-second-brain-circles.md` | Strategic items inlined here (v2.5 KG, MCPManager streaming, Notes, Brain Queue, DESIGN.md, STRATEGY.md) |
