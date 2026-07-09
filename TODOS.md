# TODOS — Renfield Master Index

Single prioritized index of every open work item, with a reference back to the source document where the original detail lives. When a topic is covered in multiple places, the **primary source** is listed first — that's the one to update when the item is actually worked.

**Tiers:**

- **P0 — Active / blocking.** Work that unblocks a commitment, gates another in-progress track, or shipped-incomplete.
- **P1 — Next substantive batch.** Ready to pick up once P0 is clear; concrete scope, no external gate.
- **P2 — Scheduled follow-ups.** Known improvements with clear scope but no forcing function yet.
- **P3 — Conditional / on signal.** Deferred pending real usage data, upstream change, or strategic green-light. Do NOT pull these forward without the trigger firing.

Long-form strategic items (formerly a separate `TODOS.md`) carry a `**WHAT/WHY/PROS/CONS/CONTEXT/DEPENDS ON**` block when the rationale is non-trivial.

**2026-06-06:** Obligation-deadline notifier + Bestätigt server-ledger shipped (`feat/obligation-deadline-notifier`) — the last load-bearing Schicht A slice. Daily owner-targeted scan + `obligation_acknowledgements` ledger + confirm/reopen endpoints + frontend rewire; WS-delivery privacy gate fixed alongside. **Per-fact tier override SHIPPED 2026-06-06** (`feat/per-fact-tier-override`) — a fact can be tiered independently of its document (public issuer on a private doc), sticky both ways, with reset; `document_facts.tier_overridden` + cascade-skip + `reset-tier` route + drawer UI. Remaining Schicht A: just the `Fakten` filter chip + `.ics` export (next PR). Re-extract-preserves-overrides is a P2 follow-up (overrides currently reset on re-OCR).

**2026-05-31:** Schicht A read layer shipped (#643 / v2.10.14) — `DocumentFactRetrieval`, `/brain` Fakt badge, facts/obligations routes, circle_sql null-KB fix, update_tier cache invalidation all marked done below; remaining Schicht A work = the **obligation notifier** + the **Fakten panel** + the **obligations agenda**. Phantom RAG reranker disabled (#644). The next actionable item is the per-document **Fakten panel** (P2, Schicht A track).

Last reviewed: 2026-05-03 (post-release sweep). Voice pipeline Phase A (v2.3.0) + Phase B-1/B-2/B-3 (v2.4.0/v2.4.1/v2.4.2) + Reva-compat bridge (v2.4.3) all landed and deployed. Frontend test suite migrated to TypeScript (#519/#520/#521) and `RoomOutputSettings` i18n leftover swept (#522). E11/E12/E13/E14/E15 audit items verified closed; W10 frontend source migration confirmed 100% complete (49 .ts + 68 .tsx, 0 .jsx). Open backlog is signal-gated, blocked, or pending external action.

---

## P0 — Active / blocking

_(no active blockers — all prior P0 items resolved and merged)_

---

## P1 — Next substantive batch

- ✅ **~~Broadcast announcement (announce to all occupied rooms)~~ SHIPPED** — `internal.broadcast_announcement` is live (`ha_glue/services/internal_tools.py:881` `_broadcast_announcement`, shared `_announce_core`, public-only fan-out over occupied rooms, `HA_CONTROL`-gated). See `docs/MESSAGE_RELAY.md` + CLAUDE.md.

- ✅ **~~#12 KB maintenance — classify unindexable vs transient 0-chunk docs~~ DONE** — already shipped in `services/kb_maintenance_tool.py` (`_unindexable_exists()` classifies REPAIRABLE vs UNINDEXABLE; `list_chunkless_documents` labels each; `reindex_documents` skips unindexable by default + `force`; `ingest_status` splits `chunkless_reindexable`/`chunkless_unindexable`). Entry was stale.
- **#10 Presence / media-follow room-switch latency (~1–2 min)** — 🚀 **LANDING ACTIVE for real-world validation** (`feat/presence-rssi-filter`): `_assign_room` now smooths per-room RSSI with an asymmetric EWMA (fast attack / slow release), decays a room the current satellite stops hearing, and switches on a **filtered enter-margin** instead of a scan count — the field-standard ESPresense/Bermuda approach. `presence_rssi_filter_enabled=true` **by default**; `PRESENCE_RSSI_FILTER_ENABLED=false` instantly reverts to the legacy raw-mean + N-scan path. **Deliberately shipped over the review's open findings** — three backend designs (adaptive fast-switch → silent-incumbent → this filter) each drew flip-flop findings across ~5 high-effort review rounds, and the tension they circle (a *decaying* incumbent over-decays and flips [F1]; a *frozen* incumbent never-switches [F4]) is a genuine tuning problem only **real satellite cadences + room layout** can resolve — so the operator's call is to validate live and tune the knobs, not to keep patching blind. **Watch-list during validation (from the last review):** (F1) a chatty adjacent satellite can erode a slow-cadence current room and steal it → tune `presence_filter_fresh_seconds` up / `alpha_down` down; (F4) an open-plan move may not clear the enter-margin → tune `presence_switch_enter_margin_db` down; (F2) a voice-set room with zero BLE sighting can flip on the first adjacent tick; (F3) a strong stray can win a first assignment; (F5) dropped multi-satellite corroboration. Tuning knobs are all env (`PRESENCE_RSSI_FILTER_ALPHA_UP/DOWN`, `PRESENCE_FILTER_FRESH_SECONDS`, `PRESENCE_SWITCH_ENTER_MARGIN_DB`) — no redeploy to tune. Bigger accuracy win deferred: **mmWave-occupancy + BLE-identity sensor fusion** (`docs/design/`, if the filter proves insufficient). See P2 detail.
- **#6 Provisioning config drift** — **active fleet DONE** (2026-07-09: Arbeitszimmer + Kinderbad host_var enrollment tokens pinned; Fitnessraum/Wohnzimmer already had them; Esszimmer uses a k8s Secret). **Remaining:** Benszimmer host_var token (deferred — offline) + inventory → mDNS + DHCP reservations (router-side). Ops (gitignored, no PR).

_(WICHTIG sweep complete. W10 closed via #487 on 2026-04-27. `tasks/audit-findings-plan.md` swept on 2026-04-30: every W1-W14 entry now points at the actual landed code/PR, and the Phase 1-4 checklists are fully ticked.)_

---

## P2 — Scheduled follow-ups

### ~~Browser wake-word: load ALL selected keywords, not just `wake_words[0]`~~ ✅ SHIPPED 2026-07-04 (PR #901)
From-scratch rework of the parked branch (which was broken). `settings.keyword` is now a comma-separated SET; the engine loads every selected model and rebuilds on a set change (never relies on `setActiveKeywords`); `wakewordEngineLoader.ts` is a mockable seam; ChatHeader is a multi-checkbox picker. Reconcile lifecycle (serialized "arm" + preemptive disable/pause via a generation guard; pause tears the engine down). 5 `/review` rounds (8→3→4→0→0 confirmed), 36 unit tests; `docs/WAKEWORD_CONFIGURATION.md` + `docs/FEATURES.md` updated. **Remaining:** post-deploy **browser E2E** — the WASM mic detection path can't be unit-verified (say "Renfield" in de+en on a real browser after the frontend deploy).

### ~~`bin/deploy-production.sh` — kill the `on_build`/`run` nested-quoting foot-gun~~ ✅ SHIPPED 2026-07-04 (PR #899)
Root-cause fix landed: `on_build` passes the remote command as a single ssh arg (remote shell quotes), and cleanup warns instead of failing the deploy. `bash -n` + quote-heavy-payload tests passed. [[reference_deploy_script_prune_bug]].

### KB maintenance tools: distinguish genuinely-unindexable docs from transiently-failed 0-chunk docs
Origin: session 2026-07-02, shipping the chat KB-maintenance tools (`internal.ingest_status` / `internal.list_chunkless_documents` / `internal.reindex_documents`, `services/kb_maintenance_tool.py`) + the browser E2E that surfaced it. All three treat every `completed`-with-0-chunks doc identically, but there are two distinct populations: (a) transiently-failed docs that WOULD produce chunks on a retry (worth reindexing), and (b) genuinely-unindexable docs — low-quality scans whose OCR is dropped by the `is_low_quality_text` quality gate on every pass, so they re-produce 0 chunks and keep reappearing in the list forever. Of the 31 chunkless docs observed, most were (b) (date-named scans, "PRIVAT INFO Rezept 2", "OpenWB Ladeprotokoll…").
- **WHAT:** Classify (a) vs (b) so the tools stop churning on unindexable docs. A doc that completes with 0 chunks AFTER a `user_reindex` (a fresh re-derivation that still yielded nothing) is genuinely-unindexable — mark it (a flag/marker, or read `document_processing_history` for attempts + `chunks_dropped_low_quality`). Then `list_chunkless_documents` labels/segregates them ("OCR unreadable" vs "never processed") and `reindex_documents` skips the known-unindexable set by default (add a `force` param to reindex anyway).
- **WHY:** Today the chat answer is honest but noisy — reindexing the genuinely-empty scans is wasted OCR and they immediately reappear as chunkless, so the count never drops and the user can't tell "fixable" from "give up / needs a better scan". Small quality-of-life gap on the just-shipped tools.
- **PROS:** the chunkless list becomes actionable (fixable vs unindexable); reindex stops churning; no false "still broken" impression. **CONS:** needs a signal for "reindexed-and-still-empty" — a new column/marker or a `document_processing_history` read per doc; the low-quality-scan population may genuinely need re-scanning at the source (out of scope) — this classifies, it doesn't fix.
- **CONTEXT:** `services/kb_maintenance_tool.py` (the three tools; chunkless query = `completed` + no `document_chunks` rows); `services/rag_service.py` (`is_low_quality_text` drop → `chunks_dropped_low_quality` recorded on the history row); `services/document_processing_history.py` (per-attempt history). Router note: these route to the `documents` role (fixed 2026-07-02, #885).
- **DEPENDS ON:** nothing; standalone follow-up on the KB-maintenance tools.

### Chat-UI modernization roadmap — progress ledger
Origin: `/plan-eng-review` 2026-06-15 on `docs/design/chat-ui-modernization.md` (survey + 3-tier
roadmap + Tier 0 cross-cutting a11y/mobile/voice-transcript/offline). Tiers: **T1** = branching(1)
· follow-up-chips(2) · message-search(3) · command-palette(4); **T2** = artifacts(5) ·
role-surfacing(6) · provenance-chips(7); **T3** = room-handoff(8) · shared-private(9) ·
gen-UI-widgets(10).

**SHIPPED + DEPLOYED (2026-06-15 → 2026-06-16):**
- ✅ **(7) Provenance source chips** — #782, ALWAYS-ON. Knowledge-backed answers show source chips (filename + `TierBadge` → `/knowledge?doc={id}`); circle-safe via `rag.search(user_id)`.
- ✅ **(2) Follow-up suggestion chips** — #784, `FOLLOWUP_CHIPS_ENABLED` (ON in prod). Best-effort small-model call in the background AFTER the `done` frame (never delays spinner/TTS/wakeword); skipped on TTS/error/short turns.
- ✅ **(4) Command palette** — #785, `COMMAND_PALETTE_ENABLED` (ON in prod). `/` or touch button → action/nav palette; tool actions STAGE into composer (no auto-send); next-turn `role_hint` is routing-only (every tool still permission-gated).
- ✅ **Correct-and-regenerate** (follow-on to 4/6) — #788. "Falsch erkannt?" also offers "Neu beantworten" → re-runs the turn forcing the corrected route (`corrected_intent` → most-specific role; reuses `role_hint`, routing-only).
- ✅ **(6) Agent-role surfacing** — #790, `ROLE_SURFACING_ENABLED` (ON in prod). Badge of the resolved role per turn; tap pins it for next turn; emitted on `done` + persisted in `message_metadata.agent_role` (rehydrates).
- ✅ **(3) Message search** — #793, `MESSAGE_SEARCH_ENABLED` (ON in prod). Postgres FTS over `messages.search_vector` (migration `pc20260617`), conversation-ownership-scoped (NOT circle_sql), jump-to-message, XSS-safe `<mark>`.
- ✅ **(5) Artifacts — Lane A** — `ARTIFACTS_TYPED_ENABLED` (ON in prod), design `docs/design/chat-artifacts-sandbox.md` (eng+design review 9/10, #796/#797/#798). Chain: **baseline CSP** Report-Only #799 → enforcing #800 (prereq); **renderer + emit plumbing** #801 (typed table/list/keyvalue/chart → React, no model HTML; `artifact` WS frame; zod authoritative shape; fail-closed escaped-code fallback; `message_metadata.artifacts[]`); **first producer** smart-home status→table #802 (`smart_home/status` sub-intent — ConfigMap `agent_roles.yaml` patched in prod). Browser-verified live. Regression fixes alongside: blob-worklet CSP (#803) + PWA-propagation build-stamp (#804). **Lane B (free-form HTML/SVG sandboxed iframe) deliberately DEFERRED (YAGNI + own security review; `ARTIFACTS_HTML_SANDBOX_ENABLED` placeholder, unwired).**

**ALSO SHIPPED (2026-06-17):**
- ✅ **More artifact producers** — #813 (backend v2.17.29): 3 more smart-home producers exercise every Lane-A kind — `smart_home/sensors`→`keyvalue`, `smart_home/active_devices`→`list`, `smart_home/devices_per_room`→`chart` (all `dispatch_sub_intent`, real `get_entity_map()` data). Live-verified: the `chart` renders a real SVG bar chart in prod. ConfigMap `smart_home.sub_intents` patched.
- ✅ **(8) Room-handoff affordance** — **BOTH halves now shipped.** #814 (backend v2.17.29 / frontend v2.15.20), `ROOM_HANDOFF_ENABLED` (dark): inline "🔊 Wiedergabe folgt nach {room}" when Media-Follow moves playback (`media_handoff` frame, room-scoped, transient). The **conversation-follows-presence (`continued`) case is now implemented** (`feat/room-handoff-continued`): `conversation_handoff.on_presence_enter_room` emits the same `media_handoff`/`kind:"continued"` frame (via `_emit_continued_frame`) to the user's new room after a successful context handoff — flag-gated/dark, room-scoped, transient. Frontend + both-locale i18n (`chat.mediaHandoff.continued`) were already in place; the only gap was the backend emit. Emits from BOTH handoff call sites — the presence-enter hook AND the satellite speak-path (`satellite_handler`) — each guarded by its own `try_handoff_context` success; the shared 10s speaker debounce yields **exactly-once** emission, so the chip appears whether the user moved-then-spoke or merely moved (the single-call-site version missed the common move-and-talk flow — caught by `/review` on #864 and fixed). 13/13 `test_conversation_handoff.py` pass on .159; ruff clean.

**NOT BUILT (remaining):**
- ✅ **(1) Message branching / edit-and-fork** (T1) — **Phase 1 + Phase 2 SHIPPED** (`CHAT_BRANCHING_ENABLED`, dark; design `docs/design/chat-branching.md`). Phase 1: conversation-tree schema (`messages.parent_message_id` self-FK + `conversations.active_leaf_message_id`, migration `pc20260618_message_branching` + idempotent backfill), recursive active-path CTE (conversation-scoped recursive step), four branch-aware seams (history / conv_context self-heal / memory-deactivate-at-fork / search filter+reindex), `save_message` tree maintenance, WS `fork_from_message_id`, `PUT …/active-leaf`, edit-latest / regenerate-latest. **Phase 2 (`feat/chat-branching-phase2`):** fork-from-ANY message + a **per-message `‹ n/m ›` switcher** (chosen over the survey's `ChatHeader` global one — handles multiple fork points; `PUT …/active-leaf` resolves to the subtree's deepest leaf) + **delete-branch** (`DELETE …/branch/{id}`; 404 unowned, 409 active-path, subtree delete, memories soft-deleted+detached to dodge the `memory_history` RESTRICT FK + atoms orphan, KG provenance detached). The one-way deactivate is replaced by a **symmetric `recompute_memory_activation`** (`is_active = source ∈ active_path`, re-derived on every fork AND switch) — adds branch-switch **reactivation** and **closes the deactivate-at-fork race** (background extraction also recomputes at commit, flag-gated). CTE-always-on; flag-off byte-identical. **/review caught + fixed:** Phase 1 — cross-conversation IDOR + conversation-delete FK; Phase 2 — the `memory_history`/atoms hard-delete FK (→ soft-delete). 26 backend (sqlite + PG) + 7 frontend tests pass.
- 🟡 **(10) generative-UI widgets** — **read-only + interactive slices SHIPPED.** Read-only (`feat/gen-ui-widgets`): a purpose-built **`weather` artifact kind** (`internal.weather_widget` → Open-Meteo MCP; `WEATHER_ENABLED`) + **agent-callable render tools** (`internal.render_table`/`render_list`, `services/widget_tools.py`) so "list/table on request" comes back as a typed widget (`chat_handler._collect_tool_artifacts` → `_emit_turn_artifacts`). **Interactive (`feat/interactive-device-widgets`):** a **`device_control`** kind — clickable light/switch toggles + scene run-buttons (`internal.device_controls` producer). First **artifact→action write-back channel**: a click → `device_action` WS frame (mirrors the Paperless-confirm card) → `chat_handler` **fail-closed `HA_CONTROL` gate** (device/satellite `user_id=None` denied under auth) → `internal.device_action` (re-validates domain/action/entity-existence; `_HANDLERS`-only, not agent-advertised). No privilege escalation (same `HA_CONTROL` as the agent path; the click deliberately skips `verify_tool_call`). **/review caught + fixed** the device-token gate hole (P0) + an entity_id cap (P1). **Round 2 (`feat/device-widgets-round2`):** **brightness slider** (lights) + **thermostat setpoint stepper** (climate added to the controllable set) — `device_action` carries an optional numeric `value`, server-clamped (brightness 0-100, temp→entity min/max; bool excluded); the producer now reads **fresh `get_states()`** (fixed the 60s-cache stale-initial-state); plus a read-only **`presence_map`** widget (rooms→present users via `internal.presence_map`). /review round 2: bool-value gate + slider-timer-unmount-cleanup + resolver-overwrite settle. All ride `ARTIFACTS_TYPED_ENABLED` (live). **Remaining:** cover/blinds position control, and richer free-form widgets gated on Lane B.
- ⬜ **(9) shared-private** (presupposes household-shared conversations, which don't exist). T3.
- Backend artifact persist is last-frame-wins per `id` (correct for whole-artifact producers; a future *streaming* producer needs server-side append).

**PREMISE CAVEAT (still open, now scoped to the remainder):** the original gate — instrument web-`/chat` share vs voice/satellite turns + an a11y/mobile baseline *before* investing — was **never formally validated**; the user directed building the first slice + most of T2 directly, and they're live. The caveat now applies to deciding whether **(1) branching** and **T3** are worth the cost: Renfield is voice-first, so confirm the web chat is a high-value surface before taking on the branching data-model rebuild. **DEPENDS ON:** nothing.

### BT-scan — deterministic per-room device-count reconciliation
Origin: live browser verification of #787 (2026-06-15, backend v2.17.20).
**WHAT:** Have the backend pre-compute the per-room device counts (named + unnamed) and the
grand total, and pass them to the LLM as structured numbers (or render them deterministically),
instead of relying on the chat model to tally `data.devices` into "N gefunden" + per-room counts.
**WHY:** The #787 fix made the scan answer room-accurate and per-room-counted (big win over the
old "only 3 of 17" + room confabulation), but on a live 25-device scan the small local model's
itemized breakdown (3 named + 5+6+4 unnamed = 18) didn't reconcile to its own stated total (25).
Cosmetic, not a regression — but a household user reading "25" then counting 18 looks sloppy.
**PROS:** numbers always add up; removes the one remaining LLM-arithmetic dependency in the BT
path. **CONS:** moves presentation logic backend-ward (the tool currently does no presentation,
by design — `bt_scan_service` docstring); a structured count block is a small contract change the
prompt must consume. **CONTEXT:** `services/bt_scan_service.py` already returns `total_devices`
+ per-device `room_best`/`rooms`; the per-room rollup is a trivial group-by on the existing data.
Tool description (`ha_glue/services/internal_tools.py` `internal.bluetooth_scan`) already instructs
the LLM to account for all devices — this replaces "instruct + hope" with computed counts.
**DEPENDS ON:** nothing; standalone, gated `BT_SCAN_ENABLED`.

### Steuererklärung prep collector (household/employee)
Origin: `/plan-eng-review` 2026-06-09. Collect + categorize tax-relevant info from the KB +
Paperless corpus per Steuerjahr, surface gaps, produce a per-Anlage dossier. Renfield
collects/organizes/flags; the human files (legal review gate). **P1 scoped LEAN** (no new
subsystem — collides with the "LLM orchestrates" rule): tax-category enum in code injected
into the Schicht A prompt → `documents.tax_categories` (JSONB, set at extraction, regenerates
on re-ingest, `isinstance`-guarded + strict-enum-validated) → a `(tax_category, year)` filter
on `document_fact_retrieval` (circle filter preserved — **regression test mandatory**) →
agent-orchestrated dossier behind `STEUER_ASSISTANT_ENABLED`. Document-level category;
year = best-effort doc date. **P1 does NOT sum** (outside-voice challenge 2026-06-09):
it groups docs by Anlage, source-links each, and flags gaps — no totals, because
`amount_value` has no sign/net convention and extraction may be incomplete (a summed
total could be directionally wrong, e.g. a reimbursement inflating a deduction). Summing
moves to P2 with per-category sign/net handling.
- **[P1-ready] Build the lean P1** (T1 enum+prompt, T2 extractor+migration+GIN index, T3 retrieval filter + no-leak regression test, T4 dossier prompt + Schicht A eval cases, T5 flag). Group + source-link + flag; **no summing**. Outside-voice-adjusted directives baked in:
  - **Payment-year caveat corpus-wide**, not §35a-only — §11 EStG Zufluss/Abflussprinzip applies to most household categories (Werbungskosten, Sonderausgaben, Spenden, haushaltsnah); the dossier states "year = document date; verify items near the Dec/Jan boundary were PAID in {year}" for all categories.
  - **Gap detection needs a mechanism** — a static situation→expected-category constant (employee→Anlage N, kids→Anlage Kind, homeowner→§35a, …) the agent diffs against found categories; the "gaps" headline can't rest on bare LLM judgment.
  - **Stamp the taxonomy year** on stored categories — German Anlage/Zeile line items change annually; without a version marker, `documents.tax_categories` skews silently across years.
  - **Bescheinigung vs receipt** — the enum/prompt distinguishes authoritative statements (Lohnsteuer-/Spenden-/Vorsorge-Bescheinigung) from incidental invoices; don't treat a payslip like a parking receipt.
  - **Test the COMPOSED WHERE clause** — the no-leak regression test must exercise the `(tax_category, year)` predicate concatenated onto `circles_clause` (AND/OR precedence), not just assert "circle filter still called."
- **[P2] Docling+poppler union extractor — blocks trustworthy sums.** `docling-drops-positioned-text-layer-tokens` (10/10): Docling silently drops some right-aligned amounts; a tax sum can be silently low. P1 mitigates via source-links + advisory framing. Real fix: union a raw text-layer extractor (poppler/pymupdf) with Docling OCR at `services/document_processor.py`. Depends on: adding poppler/pymupdf to the Docling-only backend image.
- **[P2] §35a payment-year precision (Zuflussprinzip).** Handwerker/haushaltsnahe count by payment year, not invoice date. P1 buckets by doc date + surfaces a caveat; P2 tracks the payment date and buckets by it.
- **[P2] Category-override persistence across re-extraction.** `documents.tax_categories` regenerates on re-ingest, so a manual correction is lost (same as the per-fact tier override). Mirror the `tier_overridden` sticky-bit pattern (`tax_category_overridden`) if manual correction is added.
- **[P2] Deterministic `/api/steuer/{year}` + Steuer lens (the "hybrid" tier).** Read-only endpoint summing per Anlage + the gap checklist over `document_facts`, plus a frontend lens (follow DESIGN.md — `renfield-design-system-locked`). Determinism for money where agent-eyeballed sums are risky. Promote once categories prove out.
- **[P2] Per-category deductible-figure extraction.** §35a Lohnanteil split (labor only), medical netting (minus Kassen-Erstattung). P1 sums the doc's primary/agent-picked amount.
- **[P2] Stored situation questionnaire.** P1 asks situation + non-doc inputs (commute km, home-office days) inline each time; persist a small profile if it gets repetitive.
- **[P3] CSV/PDF dossier export + ELSTER/ERiC field mapping.** Export per-Anlage; optionally map to ELSTER fields. Filing stays out of scope.

### Enable obligation → calendar sync in PRODUCTION (code shipped; blocked on calendar-MCP config)
The reconciler shipped + is deployed (v2.13.0-rc.21, 2026-06-07) but is **OFF in prod** — the Calendar MCP isn't configured there. The notifier + digest were enabled this deploy; calendar sync was held. Prerequisites to turn it on:
1. **Add `calendar_accounts.yaml` to the `renfield-mcp-config` ConfigMap** — per-user accounts (`name`/`label`/`type` ews|google|caldav/`visibility` owner|shared/`owner_id`). Currently absent (ConfigMap has only agent_roles/kg_scopes/mail_accounts/mcp_servers).
2. **Add the calendar backend credentials to `renfield-env`** — the `CALENDAR_WORK_*` / `CALENDAR_VEREIN_*` (or equivalent) username/password the `mcp_servers.yaml` calendar block reads; plus `CALENDAR_CONFIG=/config/calendar_accounts.yaml` (and mount the yaml into the pod). No `CALENDAR_*` keys exist in `renfield-env` today.
3. **Flip `CALENDAR_ENABLED=true` + `OBLIGATION_CALENDAR_SYNC_ENABLED=true`** in `renfield-env`, restart backend (Recreate + cuda gate — pre-check `cuda.local:8081/health`).
4. **Each user picks a calendar** in the agenda's "Kalender-Sync" selector (no pref → no sync; the selector is hidden until `list_calendars` returns writable calendars).
- **Verify after:** the calendar MCP appears in `GET /api/mcp/servers` (empty today); a test obligation creates an event; clearing the pref tears the events down.
- **Needs:** real calendar credentials (operator-provided). **Why P2, not done:** can't provision creds without the operator. See [[reference_deploy_set_image_pinned]] for the deploy mechanics.

### Paperless PR 4 inline follow-ups (documented in code, filed here)
All three have clear triggers but were out of PR 4 scope. Inline `# ...` comments in the relevant files carry full context.
- **Multi-replica `pg_try_advisory_lock`.** Current `asyncio.Lock` only covers a single process; k8s with >1 backend replica needs DB-level coordination. Source: `src/backend/services/paperless_ui_edit_sweeper.py` (top-of-file comment on `_sweep_lock`).
- **Multi-user attribution in ui_sweep rows.** Attribution seam exists (`_resolve_editor_user_id`); needs MCP `owner` exposure + Paperless↔Renfield user-mapping table to actually resolve the editor. Source: `src/backend/services/paperless_ui_edit_sweeper.py` (docstring on `_build_example_row` + `_resolve_editor_user_id`).
- ~~**Narrow MCP `get_document_metadata` tool or `include_content=False` flag.**~~ **MOSTLY DONE (2026-06-17 sweep):** the `include_content=False` half SHIPPED — the sweeper calls `get_document(..., include_content=False)` (paperless-mcp ≥ v1.7.0), so the truncation path is now a defensive fallback only (`_TRUNCATION_MARKER` retained for older MCP versions). The dedicated narrow `get_document_metadata` tool was NOT built and is now **low-value** (the flag already eliminates truncation). Drop unless a future need arises.

### Paperless PR 4b — No-re-edit filter (superseded flag)
If ui_sweep noise shows up in real use, mark original sweep row `superseded=true` when same field edited again later.
- **Primary source:** `docs/design/paperless-llm-metadata.md` §Implementation plan → PR 4 (scope cut) + §Database schema note on `superseded` column
- **Trigger:** noise in the corpus observable in practice. Column already exists in `paperless_extraction_examples`.

### Satellite — audio pipeline improvements
- **Primary source:** `src/satellite/TECHNICAL_DEBT.md` §Future TODOs
- **High priority:** audio preprocessing (noise reduction) on backend for resource-constrained satellites — alternative: XVF3800 hardware AEC (see `docs/XVF3800_SATELLITE.md`)
- **Medium priority:** ~~Opus audio compression (~50% bandwidth)~~ ✅ BUILT as C1 (binary WS frames, dark: `SATELLITE_OPUS_ENABLED` + satellite `audio.codec: opus`); ✅ decode moved backend→voice-server (C2 Phase 1) · echo cancellation (software WebRTC APM or XVF3800)
- **Low priority:** 4-mic beamforming extension · custom wake-word training
- **Low priority — satellite multi-core / GIL:** the satellite is a single Python asyncio process → GIL-bound, so it uses ~1.3 of the Pi Zero 2 W's 4 cores (measured 2026-07-08 on Arbeitszimmer: openWakeWord onnx pegs one core at 99.9% — it releases the GIL — while the asyncio loop + capture + opus-encode serialize onto ~one more core; ~2.5 cores idle). **Not worth fixing today:** multiprocessing would give true parallelism but (a) **RAM is the blocker** — 512 MB total, the sat already at ~200 MB / 48%, and a second interpreter reloading the onnx model would OOM; (b) IPC copy/latency on the real-time audio hot path (chunk every 80 ms through capture→wakeword→VAD→encode→send); (c) coordination complexity for no current need — the workload fits the ~1.3 cores (load avg ~1.1). Revisit only if a heavier per-satellite workload (e.g. on-device diarization/embeddings, C3) actually needs the idle cores AND the hardware gets more RAM (Pi Zero 2 W → a Pi 4/5-class or the Orange Pi Zero 3 the Esszimmer sat uses). Opus encode itself is a non-issue: measured ~10% of one core, lands on the loop thread, off the hot wakeword core.

### ~~C1 Opus — move decode from the backend to the voice-server~~ ✅ DONE (C2 Phase 1, 2026-07-07)

**RESOLVED:** decode moved off the backend WS handler onto the voice-server.
`ha_glue/services/opus_transport.py` is now a pure wire-format module; the backend
buffers the raw `[uint16 len][packet]` blob (`satellite_manager`) and forwards it
to the new voice-server `POST /api/voice/stt-opus`, where
`voice_server/services/opus_decode.py` owns the one-shot opuslib decode →
float32 mono 16 kHz PCM. opuslib/libopus0 removed from the backend image. Opus
decode now lives on the media layer alongside the browser `/ws/voice` ffmpeg
path — C1 is the on-ramp to C2 as intended. **Opus now LIVE fleet-wide** (`SATELLITE_OPUS_ENABLED=true`):
5/5 online sats negotiate opus (Fitnessraum, Arbeitszimmer, Wohnzimmer, Kinderbad, Esszimmer);
Benszimmer deferred (offline). Tests: backend 23/23 + voice-server 7/7.

**SOURCE:** architectural review 2026-07-07; `docs/design/voice-identity-wakeword-verification.md` §4 C1 / §5a D6 / §8; PRs #927 (merged C1), #928 (provisioning, merged), #930 (D6 amendment), #931 (C2 Phase 1 decode move, merged).

---

### Presence / Media-Follow — room-switch latency (~1-2 min on a genuine move)
**Tech debt from the flip-flop fix (#777, v2.17.15).** Media Follow Me now correctly follows the user *and* no longer jumps to empty rooms, but a genuine room change takes **~1-2 min** before presence switches (and the music follows). Live-measured 2026-06-14: walked back to Arbeitszimmer, switch fired ~1-2 min later.
- **Why:** the anti-flip-flop hysteresis (`presence_hysteresis_scans=2`) requires **2 consecutive scans** of the new room before switching, and the Classic-BT scan cadence is ~60-120s (`scan_interval*2`), so 2 confirmations ≈ 1-2 min. Amplified by the RSSI read being **throttled to 300s** (`classic_scanner.rssi_interval=300`) → most sightings report a flat `SYNTHETIC_RSSI=-50`, so adjacent rooms tie on strength and the system can only rely on the consecutive-scan count, not signal margin. This was a deliberate stability-over-responsiveness trade — the flip-flop (music to an empty room) was strictly worse.
- **Improvement directions (pick per cost/benefit):**
  - **Adaptive hysteresis:** switch in 1 scan when the new room's signal *decisively* beats the current (margin ≥ N dBm), else require 2. Needs real RSSI (see below).
  - **Lower the RSSI-read throttle** (300s → e.g. 30-60s) so real distance-mapped RSSI discriminates adjacent rooms instead of synthetic -50 ties. Cost: more `hcitool rssi` ACL connections per device (the throttle was added to avoid hammering the phone link); measure phone-side impact first.
  - **Faster scan cadence** on satellites (shorter `scan_interval`) so 2 consecutive confirmations happen sooner. Cost: more BT activity / battery on the Pi.
  - **Better room resolution:** BLE beacons / fixed transmitters or per-room calibration to break the adjacent-room RSSI ambiguity at the source.
- Files: `src/backend/ha_glue/services/presence_service.py::_assign_room` (hysteresis), `src/backend/ha_glue/utils/config.py` (`presence_hysteresis_scans`, `presence_stale_timeout`), `src/satellite/renfield_satellite/ble/classic_scanner.py` (`rssi_interval`, `SYNTHETIC_RSSI`).

### TTS-Audio-Auslieferung an Renderer — RESOLVED über `http://renfield.local` (deployed v2.17.2)
TTS-an-DLNA läuft über `http://renfield.local/api/voice/tts-cache/{id}.wav` (`ADVERTISE_SCHEME=http`, `backend-tts-cache-http` IngressRoute ohne Redirect). **Samsung-TVs funktionieren jetzt** (Q60CA + 8 Series gemessen ✅), Linn nativ ✅, HiFiBerry über `/etc/hosts` ✅. Der frühere Samsung-UPnP-716 war **kein** Samsung- oder dlna-mcp-Problem, sondern drei non-compliant Bits in der **Backend**-Resource (gefixt in `feat/dlna-samsung-head-mime`): HEAD→405 (Route war GET-only), MIME `audio/flac` statt `audio/x-wav` (laut `GetProtocolInfo` des TVs), keine `.wav`-Extension. https wurde zugunsten http aufgegeben (Samsung kann self-signed nicht; http ist universell). Offene Punkte:
- **[P3] `55" Interactive Signage Flip` spielt TTS nicht** — eigener Quirk (404 im dlna-mcp-`_confirm_playback_started`, beide Schemata), separat zu untersuchen.
- ~~**[P3] `provision-hifiberry.yml` CA-Schritt ist jetzt überflüssig.**~~ **DONE 2026-06-18** (`feat/schicht-a-small-trio`). CA-Tasks + `files/renfield-ca.pem` + Doc-Refs entfernt; nur der `/etc/hosts`-Pin bleibt (gstreamers getaddrinfo kann `.local` nicht über mDNS auflösen). Header/README auf http aktualisiert. `/etc/hosts` wird bei OS-Update zurückgesetzt → Playbook nach Update neu laufen lassen.
- **[P3] dlna-mcp default-audio-MIME ist `audio/flac`** (`metadata.py:_DEFAULT_AUDIO_MIME`) — falsch für nicht-flac-Resources. Für TTS jetzt umgangen (Renfield gibt `mime_type=audio/x-wav` mit), aber andere Caller (Jellyfin) treffen den Default. Sauber: aus der URL-Extension ableiten oder neutraler Default.
- **[Tech debt] TTS-an-Renderer läuft bewusst unverschlüsselt über http.** Der self-signed-CA/https-Pfad (und der HiFiBerry-CA-Install, entfernt 2026-06-18) wurde fallengelassen, weil Samsung-TVs self-signed https ablehnen. Folge: TTS-Audio **und** die `tts-cache`-URLs gehen im Klartext über `http://renfield.local/...` an **alle** Renderer, und die Renderer authentifizieren das Backend nicht. Für ein vertrauenswürdiges Einzelhaushalt-LAN (offline-first-Ethos) akzeptabel — aber ein **bewusster Security/Privacy-Downgrade**, hier getrackt damit die http-Wahl nicht später als Versehen (re-)„gefixt" wird. **FIX** falls sich die Anforderungen verschärfen (untrusted/shared LAN, oder Auslieferung über Netzgrenzen): ein von den Renderern tatsächlich vertrauter Cert-Pfad — eine interne CA auf allen Renderern vorinstalliert, ODER per-Renderer-Capability-Erkennung (https für Linn/openHome, die es nativ akzeptieren; http nur für Samsung), ODER ein ACME/Let's-Encrypt-Cert auf einer internen Domain. **CONS:** CA-Verteilung/-Rotation auf Buildroot-Renderern (HiFiBerryOS) ist genau der Aufwand, den der CA-Schritt verursachte; per-Renderer-Verzweigung verkompliziert den Auslieferungspfad. **Trigger:** sobald das LAN nicht mehr voll vertrauenswürdig ist oder die TTS-Auslieferung das lokale Netz verlässt.

### Browser TTS barge-in cutoff (browser-played answers truncate after sentence 1)
Origin: 2026-06-17 voice-TTS debugging (PRs #805/#806). **WHAT:** when a turn's TTS plays in the **browser** (the frontend `useVoiceStream` sentence-chunked path — i.e. a device with NO room output device, e.g. mobile/laptop or a renfield-only room), the open mic can hear sentence 1 over the speaker, the VAD treats it as speech, and **barge-in cancels the rest of the TTS** → the answer cuts off after the first sentence. **WHY it's mostly latent now:** rooms with a real audio device route the FULL clip server-side to that device (#806), so the browser doesn't stream-play there; the cutoff only bites the browser-only/mobile path. **DIRECTIONS:** (a) suppress/duck the mic (or raise the VAD floor / require a higher barge-in confidence) while local TTS is actively playing; (b) rely on the browser's echo cancellation — already on, evidently insufficient on some devices; (c) play browser TTS as one full clip (the `POST /api/voice/tts` path, which returns a single decodable WAV — verified) instead of sentence-chunked streaming, so there's no per-sentence cancellation seam. **CONS:** (a) risks dropping a *genuine* barge-in; (c) loses streaming's lower time-to-first-audio. **CONTEXT:** frames themselves are clean (each decodes at 48kHz — verified via a `/ws/voice` probe); this is a playback/echo issue, not a data issue. `src/frontend/src/pages/ChatPage/hooks/useVoiceStream.ts` (`drainQueue`, barge-in generation). **DEPENDS ON:** nothing; gated to the browser-playback path. See `reference_chat_tts_dlna_routing_gap` / `reference_voice_tts_send_race`.

### Output routing — explicit per-device "mobile / never-route" flag
Origin: 2026-06-17 routing-policy clarification (PR #807). **WHAT:** "stationary vs mobile" is currently inferred from whether the requesting device's IP is **registered as a room device** (`resolve_room_context_by_ip`) — a roaming phone/laptop hits no room → plays in the browser, which satisfies the policy today. Add an **explicit per-device flag** (e.g. `mobile`/`never_route` on the device or output-device row) so a device the user *does* register to a room but that sometimes moves (a portable tablet) can be pinned to always play locally, never routed to a room speaker. **WHY:** the IP-registration proxy is correct for the common case but can't express "registered to a room AND mobile." **CONS:** a new column + a small config/UI control + the routing must consult it before the room-device selection. **CONTEXT:** routing entry is `ha_route_chat_tts_to_device_output` (returns False → browser) + `OutputRoutingService.get_audio_output_for_room`; the flag would force the False/browser path for that device. **DEPENDS ON:** nothing. **TRIGGER:** when a user actually registers a sometimes-moving device to a room and wants it to stay on browser.

### EMPFEHLUNG audit findings — modernization + cleanup
- **Primary source:** `tasks/audit-findings-plan.md` §EMPFEHLUNG, §Priorisierte Roadmap Phase 4-5
- **Frontend:** ~~W9 React.lazy code-splitting~~ · ~~W11 Prettier~~ · ~~E11 React Query~~ (all 23 list-fetching surfaces migrated: #504 foundation + bulk + #505 final on 2026-04-30) · ~~E12 13 hardcoded German strings~~ (closed; `RoomOutputSettings` i18n leftover swept in #522 on 2026-05-03) · ~~E13 ChatPage prop drilling → Context~~ (verified done — ChatInput takes 0 props, all from ChatContext) · ~~E14 ESLint React version~~ · ~~E15 enable `tsconfig` strict mode~~ (closed on 2026-04-30; 15 errors fixed; final 5 strict-mode tail errors closed in #519 on 2026-05-03 — 100% strict, 0 errors) · W10 closed via #487 on 2026-04-27, full frontend TypeScript including test suite migrated in #520/#521 on 2026-05-03
- ~~**i18n follow-up (out of E12 scope):** `RoomOutputSettings.tsx` ~10 hardcoded German strings.~~ **DONE — swept in #522** (`56af809`, 2026-05-03; this entry was stale + duplicated the line above). Verified 2026-06-17: 39 `t('rooms.outputDevice*')` calls, all labels/options/selectors localized, zero untranslated literals. Closed out.
- **Backend/config:** ~~E1-E3 speaker-loading + eager-load cleanup + FK indexes~~ (verified: per-speaker embedding cap enforced on write; KB listing uses count subquery; all 4 FK columns carry `index=True`) · ~~E4-E9~~ · ~~E10 frontend localhost fallbacks~~ · ~~E16 legacy config field removal~~ · ~~E17 Redis URL parameterization~~ · ~~E18 Frigate MQTT defaults~~

### Self-Learning Admin Console — follow-ups (out of v2.10 PR scope)
The main PR ships Skills Inbox + Tool-Health + Trajectories + Curator Runbook with the `status` enum and draft-pool gating. These items were explicitly deferred from that scope per the 2026-05-26 `/plan-eng-review` of the admin console PR.

- **Tool-Health charts.** v2.10 PR ships table-only (per-user, per-tool success/failure with rolling failure summary). Add trend lines (success-rate over time, failure-cluster heatmap) once we have ≥30 days of `tool_outcome_stats` data to chart. Backend already records `last_failure_at` + counters; needs a time-series view.
- **Trajectory v1/v2 diff view.** `memory_v2_shadow_log` already captures v1-vs-v2 dispatcher outcomes for the same turn — separate substrate from `agent_trajectories`. Build a side-by-side diff page (`/admin/memory-v2-shadow`) gated until the Phase B flip lands (`memory_extraction_v2_authoritative=True`). Not in the admin-console PR because it's a different subsystem.
- **Bulk approve / multi-select in Skills Inbox.** v2.10 ships single-row approve/reject only. Add multi-select + bulk-approve once we have evidence (≥2 weeks burn-in) that the queue actually accumulates fast enough to justify the UI complexity. Outside voice in the eng review specifically flagged this as a "wait for data" decision.
- **Playwright `--host-resolver-rules` config for CI.** During v2.9.1 post-deploy smoke we hit a Chromium-via-Playwright DNS quirk: Chromium's net stack didn't honor the system resolver for `renfield.local` on XHR/WebSocket calls even though `/etc/hosts` had the entry. Local workaround (`/etc/hosts` already present) lets dev browser sessions work, but the v2.10 PR's E2E Playwright suite needs an explicit config-file approach (`--config <path>` with `browser.launchOptions.args = ["--host-resolver-rules=MAP renfield.local 192.168.1.230"]`) for any CI / clean-machine runner. Investigation needed: confirm the actual JSON schema Playwright MCP accepts (my first guess `browser.launchOptions.args` didn't take effect; either schema was wrong or MCP didn't reload).
- **Swap `procedural_skills.status` composite indexes to partial indexes after rollout.** `idx_procedural_skills_status_user` and `idx_procedural_skills_tier_status` (added in `pc20260527`) are plain B-trees over a 4-value text column. Once the draft-gate is live and the corpus skews `approved`-heavy, plain B-tree selectivity will be poor — Postgres will fall back to seq-scan for the hot `find_similar` path. Replace with `WHERE status = 'approved'` partial indexes (and a smaller `WHERE status = 'draft'` partial for the admin Skills Inbox query). **Trigger:** ~30 days of post-v2.10 burn-in once the per-status distribution is observable in `EXPLAIN ANALYZE`. ~1 h of work — one mini-migration with the new partial-CREATE + drops; rollback-safe. **Source:** [docs/TECHNICAL_DEBT.md #12](docs/TECHNICAL_DEBT.md) + the original `/review` finding on PR #615.

**Trigger check (measured on prod 2026-07-05 — all five still parked):** none of the triggers has fired, and the prod data explains why. Tool-Health charts: `tool_outcome_stats` = **3 rows** over a 9-day window (stale since 2026-06-17), no time-series to chart. Trajectory v1/v2 diff: still `MEMORY_EXTRACTION_V2_SHADOW=true` (NOT authoritative) — Phase B gate unmet. Bulk-approve: `procedural_skills` = **5 rows, all `approved`, 0 drafts** — the Skills Inbox never accumulates. Partial indexes: 5 rows total → the "at scale" premise is absent (zero benefit; would be pure churn). Playwright CI config: low value — CI is intentionally non-functional. **Root cause (the real finding):** the self-learning loop is **data-starved because the speaker→user identity pipeline is set up but unused.** Speaker recognition auto-enrolls but nobody named/linked the voices: **38 speakers, all "Unbekannter Sprecher", 0 named; only 1 of 3 users linked** (`users.speaker_id`). ~half of recent voice turns recognize a speaker, but it's an unknown/unlinked one → the turn resolves to no `users.id` → `agent_trajectories` / `tool_outcome_stats` / skills stay anonymous. Fixing this is an **operational** task (merge the fragmented unknowns + name + link in `/speakers`), NOT a build — and it was **blocked by a bug**: speaker delete/merge 500'd on a FK violation (all three FKs to `speakers.id` were `NO ACTION`). That's fixed (`fix/speaker-delete-fk-actions`, migration `pc20260705` — embeddings CASCADE, conversations/users SET NULL; merge reassigns+preserves). Next lever if the loop should actually accumulate: consider recording tool *health* for anonymous turns (null-user bucket — tool health is about the tool, not the user).

### Speaker recognition — enrollment redesign (IN PROGRESS, design approved)
Investigating the identity gap above (2026-07-05) found speaker recognition operating **near its noise floor**: measured same-speaker cosine **0.275** vs different-speaker **0.224 p95** (healthy far-field ECAPA: same 0.6–0.85, diff 0–0.2). The match threshold **0.25 sits inside the overlap** → it both false-merges different people AND fails to match a person to their own profile, and `speaker_continuous_learning` appends every turn (incl. wrong matches) → a self-reinforcing pollution loop (38 fragmented "Unbekannter Sprecher" for 3 people). Threshold/merge tweaks are lipstick; the real fix is **controlled, quality-gated enrollment** + stopping ambient auto-enroll. **Design (approved): `docs/design/speaker-enrollment-redesign.md`.** Decisions: unknown→quality-gated review bucket; enroll via the **voice-server ONNX** (same model as inference — the backend SpeechBrain enroll path is in a DIFFERENT embedding space); **hybrid** enroll mic (close-mic seed + later far-field adaptation); strict gates (household=3).
- ✅ **Phase 0 SHIPPED** (`feat/speaker-quality-gating-phase0`, dark `speaker_quality_gating_enabled`): L2-normalize before averaging the reference centroid; skip auto-enroll + continuous-learning for too-short turns (`speaker_recognition_min_duration_s`, best-effort — voice-server HTTP paths pass `audio_duration_s`; the WS frame doesn't yet); only reinforce a profile on a strong match (`speaker_continuous_learning_min_confidence`). Flag off = byte-identical.
- ⬜ **Phase 0.5:** thread `audio_duration_s` onto the chat-WS frame (`WSChatMessage` + voice-server `final_transcript` + frontend) so the duration gate covers the PRIMARY (satellite→voice-server→WS) path, not just the HTTP paths.
- ✅ **Phase 1 SHIPPED** (`feat/speaker-controlled-enrollment-phase1`): `services/speaker_enrollment_service.py` — multi-sample enroll via the voice-server ONNX (`voice_server_client.stt`), duration + count + **cohesion** gates (mean pairwise cosine ≥ `speaker_enroll_min_cohesion`; reject = don't store), creates a named `enrolled=True` speaker (migration `pc20260706`), links a user (UNIQUE-safe). `POST /api/speakers/enroll` (multipart, SPEAKERS_ALL). `bin/purge_unknown_speakers.py` (dry-run/--commit; never deletes enrolled). 7 PG tests; 125 speaker-suite green.
- ✅ **Phase 2 (frontend) SHIPPED** (`feat/speaker-enrollment-phase2`): `components/speakers/GuidedEnrollModal.tsx` — guided multi-take recorder (record N samples via `getUserMedia`/`MediaRecorder`, ≥3, user-link dropdown), POSTs to `POST /api/speakers/enroll` via `useControlledEnroll`; shows accept (cohesion) / rejection (actionable reason to re-record). webm works E2E (voice-server ffmpeg auto-detects it). Wired into SpeakersPage + de/en i18n; TS-clean, prod build + 2 RTL tests + existing SpeakersPage suite green. **Also (live experiment):** corrected the noise-floor diagnosis — a clean XVF3800 capture gives 0.70 cohesion; the 0.28 was polluted-profile artifact, validating this approach.
- ⬜ **Operational (yours):** in `/speakers`, use "Sprecher einlernen" to enroll the 3 members (close-mic, guided); then `python bin/purge_unknown_speakers.py --commit` to wipe the 41 polluted "Unbekannter" profiles. (Deploy Phase 1+2 first.)
- ✅ **Phase 3 (controlled recognition + review bucket) BUILT dark** (`feat/speaker-phase3-controlled-recognition`, `speaker_controlled_enrollment_enabled`): the resolver identifies against **enrolled profiles only**, requires the best match to beat the runner-up by `speaker_match_min_margin`, keeps reference profiles **immutable** (no passive reinforcement), and on a miss routes a quality-passing unknown to the `speaker_candidates` **review bucket** (capped `speaker_review_bucket_cap`) instead of auto-enrolling. Admin API `GET/POST /api/speakers/candidates{,/promote,/dismiss}` (SPEAKERS_ALL); `promote_candidates` reuses the enrolled-speaker store/link path (FOR-UPDATE-guarded against dup-promote) with the same cohesion+count gates. Migration `pc20260707` (`speaker_candidates`, FK `SET NULL`). Non-finite wire embeddings rejected up front; chat-WS voice path now threads `speaker_audio_duration_s` so the short-turn gate fires there too. Flag off = resolver byte-identical. `/review`: 6× P2 all fixed pre-merge. Tests: resolver controlled behavior + promote gates + route-shadow guard, real PG.
- ✅ **Phase 3b (review UI + calibration) BUILT** (`feat/speaker-phase3b-review-ui`): `components/speakers/ReviewBucketSection.tsx` on `/speakers` (self-hiding until candidates exist) lists unmatched voices → promote selected to a named enrolled speaker / dismiss noise (over the Phase-3 endpoints); `bin/calibrate_speaker_threshold.py` recommends threshold+margin from the household's OWN enrolled voices, measured the same way the resolver scores (cosine(sample, centroid); no number when profiles overlap). `/review`: 1 P1 (silent promote/dismiss HTTP failures) + geometry/overlap/flash P2s, all fixed pre-merge. 4 RTL green.
- ⬜ **Phase 3 flip (operational, blocked):** enroll ≥ 2 household members via `/speakers`, run `bin/calibrate_speaker_threshold.py`, set `speaker_recognition_threshold` + `speaker_match_min_margin` from its recommendation, then flip `speaker_controlled_enrollment_enabled` on. No code left — gated on the household enrolling (prod has 1 enrolled today).
- ⬜ **Phase 4 (optional):** far-field adaptation; AS-norm/score-normalization if separation still marginal; upstream capture (satellite noise-suppression / XVF3800 AEC+beamforming before ECAPA).

### Brain quality — follow-ups (out of v2.10.4 PR scope)
v2.10.4 shipped the per-chunk OCR-quality gate at ingestion + the doc-level re-OCR convergence trigger. The cleanup of the EXISTING corpus and operator-facing tooling were explicitly deferred per the 2026-05-26 `/plan-eng-review` of the brain-quality plan (subagent flagged 4 correctness issues in the cleanup script that justify splitting).

- ~~**Cleanup script `bin/purge_low_quality_chunks.py` with the 4 correctness guards.**~~ **SHIPPED (2026-05-27, branch `feat/ocr-cleanup-history`).** History-table architecture (`document_processing_history` via `DocumentProcessingHistoryService`) plus the script. Guards delivered: (a) ✅ `parent_chunk_id` CASCADE in `pc20260530`. (b) ✅ Two-layer lock — `pg_try_advisory_lock` (script-vs-script, dedicated asyncpg connection bypasses the engine's checkin-hook that drops advisory locks) + `SELECT FOR UPDATE NOWAIT` on the documents row (script-vs-API). (c) ✅ `has_force_ocr_succeeded(doc_id)` over the partial index — idempotent across runs, with zombie-row safety (status='processing' doesn't count). (d) **Deferred** — startup sweep is documented in the service docstring; the cleanup-script's idempotence guard already covers crashed-mid-batch zombies on the next run, so the lifecycle.py change is cosmetic and out of scope for v1.
- ~~**Admin UX for low-quality OCR documents in the Paperless Audit page.**~~ **SHIPPED 2026-06-17** (`feat/low-quality-ocr-admin-ux`). New **Niedrige OCR-Qualität** tab + inline OCR-tab badge: a doc is flagged when its renfield `documents` row has `status='failed' AND error_message LIKE 'ocr_quality%'` OR its latest `document_processing_history` dropped ≥30% of chunks (`dropped/(produced+dropped) ≥ 0.30`). Signal joined paperless_doc_id→`Document.paperless_document_id`, batch-resolved one query/page (latest-history via `DISTINCT ON`). New `documents.quality_ignored` (migration `pc20260618_doc_quality_ignored`) + `POST /api/admin/paperless-audit/quality-ignore` (ADMIN) + `low_quality_only` filter; the cleanup script (`bin/purge_low_quality_chunks.py`) skips ignored docs. Actions: Erneut OCR (reuses the existing local re-OCR path) + Ignorieren/Wieder berücksichtigen. 103 backend tests + frontend tests; review clean. See `docs/FEATURES.md` → Paperless Audit.
- **OCR engine evaluation / swap.** WHAT: benchmark the current OCR engine on the historical garbage docs; if an alternative reduces failure rate by >50%, plan the migration. WHY: root cause of the OCR quality problem is upstream engine quality; everything else (heuristic filter, re-OCR trigger, retrieval filter) is a layered workaround. **NOTE (2026-06-17 sweep): premise refreshed** — the engine is **already docling-based** (`document_processor.py`: docling `OcrAutoOptions`/`EasyOcrOptions`, `ocr_engine` ∈ `docling`/`docling_full_page_ocr`/`poppler_text_layer`), NOT the bare `easyocr` the old WHAT assumed, so the "easyocr→docling-default" comparison is moot; reframe as "docling-OCR vs tesseract/cloud" if pursued. **Cons:** offline-first ethos limits cloud options; days of benchmarking + integration. **Trigger:** scale-driven — defer until ≥hundreds of operator-flagged quality failures justify the cost.

### Schicht A field extractor — follow-ups
The hybrid extractor (deterministic Steuernummer/IBAN with whitespace normalization + LLM obligations/universal facts) landed as a `post_document_ingest` consumer storing `document_fact` atoms. Opt-in: `schicht_a_extraction_enabled` (now ENABLED in prod). **Read layer SHIPPED in #643 / v2.10.14 (2026-05-31):** `DocumentFactRetrieval` (FTS + identifier-ILIKE + `facts_for_document` + `obligations`), `document_fact` fused into `/brain` RRF (green "Fakt" badge), the `update_tier`→`invalidate_for_atom` fact-cache fix, the circle_sql null-KB owner fallback, and the `GET /api/atoms/documents/{id}/facts` + `/api/atoms/obligations` routes. Remaining items below are the **UI surfaces + the proactive notifier** (the read path they sit on is now live).

- ~~**Fact retrieval (`DocumentFactRetrieval` + `/brain` RRF + `update_tier` cache invalidation).**~~ **SHIPPED #643 / v2.10.14.** Facts are queryable; the resolver-invalidation note is delivered (T5). ~~The agent-context wiring (facts in the ReAct agent's retrieval context, not just `/brain`) is the one unbuilt slice.~~ **AGENT-CONTEXT WIRING SHIPPED** (`feat/schicht-a-agent-fact-wiring`) — `internal.knowledge_search` now runs `DocumentFactRetrieval` alongside `rag.search` (gated on `schicht_a_extraction_enabled`) and folds circle-filtered facts into a `FAKTEN` block in `data.context` + `data.facts`, so "what's my Steuernummer" cites the normalized fact, not the chunk. `/review` caught + fixed a circle leak (the fact-source-document **title** lookup was unfiltered → a tier-overridden-public fact leaked a private doc's title/filename; now `_visible_document_meta` circle-filters it, generic `Dokument {id}` + no chip for non-visible sources). Flag-off = chunk-only byte-identical. See CLAUDE.md internal-tools table.
- ~~**Obligation-deadline notifier — the load-bearing remaining half.**~~ **SHIPPED 2026-06-06** (`feat/obligation-deadline-notifier`). `services/obligation_deadline_notifier.py`: one daily idempotent owner-targeted scan over `document_facts` obligations computes the single current lead-time milestone (`14d`/`7d`/`3d`/`1d`/`due`/`overdue`) and fires it once via `NotificationService`, recording a `(fact, user, milestone)` row in the new `obligation_acknowledgements` ledger so a pod restart never re-fires (run_at_boot; the missed-deadline safety property). Legal-gate kinds notified but flagged human-gated (message → `/brain/review`, urgency raised), never auto-acted. Gated on `obligation_notifier_enabled` AND `proactive_enabled` (opt-in, dark). Design per `schicht-a-obligations-source-of-truth` (no Reminder rows / no chat-reminder loop). **Delivery privacy fix shipped same PR:** `ha_deliver_notification` now presence-gates the WS broadcast (not just TTS) so a `privacy="personal"` reminder can't fan out to all household devices.
    - ~~**Migrate the agenda's localStorage Bestätigt state onto this ledger.**~~ **DONE same PR** — `POST/DELETE /api/atoms/obligations/{id}/confirm` (circle-gated, per-user), `obligations()` carries `confirmed`, `useBestaetigt` rewired to the server (optimistic override + 5s undo + onError rollback) with a one-time localStorage→server migration. A `confirmed` ack also suppresses the owner's further milestones.
    - ~~**Follow-up (P2) — weekly catch-all digest (the safety floor).**~~ **SHIPPED 2026-06-06** (`feat/obligation-digest`). `services/obligation_digest.py` (`_schedule_obligation_digest`, weekly, run_at_boot): once per ISO week sends each owner ONE summary of every OPEN obligation **with no lower date bound**, so a document OCR'd past the notifier's grace window still surfaces (closes F3). Deduped `(user, ISO-week)` via the new `obligation_digest_log` (restart-safe; dedicated table, not the TTL-reaped notification). Owner-targeted, opt-in `OBLIGATION_DIGEST_ENABLED` + `PROACTIVE_ENABLED`. Also fixed in the same PR: `NotificationService._compute_dedup_key` now includes `target_user_id` (two members' identical digests/reminders no longer cross-dedup). The *never-extracted* case stays out of scope (must remain observable upstream).
- ~~**Frontend / GUI to surface facts — the three layered surfaces.**~~ **ALL THREE SHIPPED** (`/brain` in #643; the two dedicated surfaces built on `feat/schicht-a-gui`, eng + design + `/review` cleared 2026-06-01).
    1. ~~**Obligations agenda** (`/brain/fristen`)~~ **SHIPPED** — deadline inbox grouped by urgency (Überfällig / Diese Woche / Später) off `obligation_date`, `legal_gate` kinds flagged `⚑ rechtlich`, **Bestätigen** with undo toast (now server-backed via the obligation ledger — see the notifier item above; localStorage migrated away 2026-06-06). Reads `GET /api/atoms/obligations` (+ `offset` paging for "Mehr laden"). Pairs with the obligation notifier (item above).
    2. ~~**Per-document facts panel** (`/knowledge` document cards)~~ **SHIPPED** — inline `FaktenPanel` (lazy-fetch on expand) listing issuer / Eckdaten / Kennzeichen / Fristen, Paperless-metadata style, with three empty states gated on the new `GET /api/config/features` flag. Reads `GET /api/atoms/documents/{id}/facts`. Bidirectional deep-link with the agenda (`?doc={id}#fakten` ↔ `#frist-{id}`).
    3. ~~**Facts in `/brain` unified search.**~~ **SHIPPED #643** — `document_fact` is a fused RRF atom type with a green "Fakt" badge (`BrainPage`/`BrainReviewPage` + de/en i18n). ~~A `Fakten` filter chip was NOT built~~ **`Fakten` filter chip SHIPPED** — `BrainPage` `factsOnly` toggle + `({factCount})`, de/en i18n `circles.filterFactsOnly` ("Nur Fakten"/"Facts only"). Closed out 2026-06-17.
  Delivered cross-cutting: shared `FactProvenance` (`✓` deterministic / `~` advisory + low-confidence hint) + `ObligationRow` + `TierBadge` on every fact; `.fact-group` / `.legal-flag` / `.toast` / `.atom-row--bestaetigt` tokens (DESIGN.md-compliant, `--color-cream`, dark + reduced-motion); full de/en i18n. **Proactive calendar auto-push (Calendar MCP) — SHIPPED 2026-06-06** (`feat/obligation-calendar-sync`). Per-user opt-in reconciler (`services/obligation_calendar_sync.py`, daily, run_at_boot, gated on `obligation_calendar_sync_enabled` + the Calendar MCP): mirrors each opted-in user's open obligations into their chosen calendar — create/update/delete via the MCP, tracked in the `obligation_calendar_events` ledger (fact→event_id, FK SET NULL for orphan cleanup). `obligation_calendar_pref` holds the per-user calendar choice; `GET/PUT /api/atoms/obligations/calendar-pref` (clearing tears down the user's events first). Owner-scoped (MCP enforces per-calendar access by user_id), advisory-locked, op-capped, not-found-delete idempotent. **P2 follow-up:** the MCP has no idempotency key, so a crash between a successful `create_event` and the ledger commit can leave a duplicate event (at-least-once) — close with a pre-create marker-scan or MCP idempotency support. Also: events are timed (all-day unsupported by the MCP) at `obligation_calendar_event_hour`.

**Schicht A UI surfaces — ALL SHIPPED (closed out 2026-06-17; the prior "still deferred" note was stale):** ~~the `Fakten` filter chip on `/brain`~~ **DONE** (`BrainPage` `factsOnly` toggle + factCount + de/en i18n); ~~`.ics` export~~ **DONE** end-to-end (`GET /api/atoms/obligations/export.ics` backend — circle-filtered VCALENDAR, RFC-5545-escaped, one all-day VEVENT per dated obligation — + the agenda's `buildObligationsIcsUrl(...)` download link in `ObligationsPage.tsx` + `obligations.exportIcs` i18n). ~~per-fact inline `TierPicker`~~ **SHIPPED 2026-06-06** as a real per-fact tier *override* (`document_facts.tier_overridden`, sticky both ways, reset route + drawer UI) — not the naive "tier follows the doc" version; see the 2026-06-06 header note. ~~**P2 follow-up:** preserve per-fact overrides across re-ingest/re-OCR (currently a re-extraction recreates facts fresh and resets overrides to the doc tier).~~ **SHIPPED 2026-06-15** (`feat/fact-tier-override-carryover`) — the Schicht A ingest hook now snapshots the prior overridden facts by content identity (`schicht_a_extractor._fact_identity_key` = `category` + `kind` + normalized/value signature, `_squish`-ed so OCR letter-spacing/case drift doesn't break the match) BEFORE writing the fresh set, and re-applies `tier_overridden` + the override tier to a matching re-extracted fact. No migration (reuses `tier_overridden` + `circle_tier`). A fact whose content drifted enough not to match reverts to the doc tier (fail-safe: never more visible than the parent doc by default); a reset (cleared override) does not resurrect. Tests: `tests/backend/test_fact_tier_override_carryover.py` (override survives re-extraction · non-overridden follows doc tier · drift reverts · reset stays cleared + the identity-key unit tests).
- **`/brain` may show a fact and its source chunk as two results (dedup-if-noisy).** WHAT: a `/brain` query can match both a `document_fact` (e.g. issuer) and the `document_chunk` it was extracted from; they have different `atom_id`s so RRF won't merge them → two results for one underlying fact. Decided in the DocumentFactRetrieval eng-review (Finding 2) to **accept + observe** for v1 (the fact is the precise answer, the chunk is context; the green "Fakt" badge differentiates them). FIX if noisy: collapse a `document_fact` and a chunk sharing the same `document_id` post-RRF, preferring the fact. **Cons:** adds cross-atom-type linkage logic to `_rrf_merge`/`query()` and can hide a genuinely relevant chunk. **Trigger:** when real `/brain` usage feels noisy with fact+chunk pairs.
- **pg_trgm index for identifier search at scale.** WHAT: `DocumentFactRetrieval.search()` adds a `normalized_value ILIKE :raw` branch (gated on identifier-shaped query tokens) for exact Steuernummer/IBAN/Aktenzeichen lookup. Even gated, ILIKE is unindexed (leading wildcard → seq-scan over the matched fact rows). FIX at scale: `CREATE EXTENSION pg_trgm` + a GIN trigram index on `document_facts.normalized_value` to make the branch index-assisted. **Cons:** new extension dependency + a second GIN index on the table; trigram has its own short-token fuzzy quirks. **Trigger:** when identifier-search latency becomes measurable at large corpus scale (negligible at household scale today). From the DocumentFactRetrieval eng-review (Finding 1).
- **Cheap pre-filter before the LLM obligation pass.** WHAT: `SchichtAExtractor.extract` calls the LLM for **every** ingested doc when enabled (one `num_predict=1200` classification call, serialized after the KG hook in the same `post_document_ingest` task). Gate the LLM pass on a cheap signal (doc has a date/amount/Frist-keyword, or is a letter/invoice type) so junk/photo docs skip it. WHY: at bulk-import scale this doubles per-doc LLM latency and queues background tasks. **Cons:** an over-eager filter costs recall (the safety axis) — calibrate against the local golden set. **Trigger:** when a real corpus is enabled and per-doc ingest latency or LLM queue depth becomes measurable.
- ~~**Concurrent same-doc reindex can leave duplicate facts.**~~ **FIXED** (`feat/schicht-a-reindex-lock`). The write-then-purge critical section is now wrapped in a per-document Postgres advisory lock (`pg_try_advisory_lock`, NS `0x5341`) on a **dedicated connection** (a session-level lock drops at the mid-hook commit); the loser skips (the winner's fresh set is a complete refresh). `/review` caught that the second pooled connection could add pressure under a folder-ingest backlog (the 2026-07-01 outage class), so lock-connection acquisition has a bounded timeout and **degrades to unlocked** under pool pressure rather than blocking (worst case reverts to the pre-existing rare-duplicate, reconciled next reindex). No-op on sqlite. Tests (real PG): skip-when-locked + two concurrent hooks leave exactly one fact set.
- ~~**`amount_currency` is truncated, not validated.**~~ **DONE 2026-06-18** (`feat/schicht-a-small-trio`). `_clean_currency` now maps common symbols/words (€/$/Euro→EUR…) then validates against a static ISO-4217 alpha-3 set; a hallucinated/non-currency string returns None (field nullable) instead of being stored. `TestCleanCurrency` covers valid/alias/invalid.
- **Proper cross-encoder reranker (rerank currently DISABLED).** WHAT: `RAG_RERANK_ENABLED` was set to `false` in `k8s/configmap.yaml` (2026-05-31) because the default `RAG_RERANK_MODEL=mxbai-rerank-base-v1` is not in the Ollama registry (never pullable → always 404 → silent fallback to RRF order), AND `rag_retrieval._rerank` is a **bi-encoder cosine** pass (`client.embeddings(model=...)`), not a true cross-encoder. So "reranking" never actually ran. WHY: RRF + lexical fusion is the real ranking today; a genuine cross-encoder reranker would improve top-K ordering. FIX: stand up a cross-encoder rerank endpoint (e.g. a dedicated reranker served via a rerank-capable stack, or a `/rerank` HTTP service) and rewrite `_rerank` to call it with (query, candidates) → relevance scores, instead of embedding each side and cosine-ing. **Cons:** new serving dependency + code change; bi-encoder repoint to an existing embed model (e.g. `nomic-embed-text`) was rejected as it could degrade a strong 2560-dim primary. **Trigger:** when retrieval top-K ordering quality becomes a measured pain point.
- ~~**`lang` is not plumbed to the `post_document_ingest` firing site.**~~ **DONE 2026-06-18** (`feat/schicht-a-small-trio`). `RAGService.process_existing_document` now passes a detected `lang` to `run_hooks(...)`. NB: the spec's "it is detected at ingest" was inaccurate — Renfield stores **no** per-document language and there was no detector, so this added one: `detect_document_language(field_text, default)` (pure-Python `langdetect`, clamped to languages with prompt variants `{de,en}`, falls back to `settings.default_language` on short text / detector error / unsupported language). `test_rag_lang_detect.py` covers en/de/fallback. **Follow-up if needed:** store the detected language on `documents` (a column) so other consumers/UI can reuse it instead of re-detecting; and broaden the supported set if more prompt-language variants are added.
- ~~**KG hook accumulates entities on re-ingest (no pre-delete by source).**~~ **CLOSED as obsolete + not-safely-implementable (investigated 2026-07-05).** The premise ("re-indexing accumulates duplicate KG entities") predates the dedup logic that now prevents row duplication: `resolve_entity` dedups entities by exact-name/surface-form (same names re-resolve to the same row) and `save_relation` dedups relations **globally** on `(subject, predicate, object, is_active)` (a re-produced triple reuses the existing row, only bumping confidence). So re-ingesting a document does **not** create duplicate entity or relation rows today. And the suggested fix ("delete by `source_ref`") is **unsafe**: (a) `kg_entities` has **no** per-document provenance column at all → a delete can't even be scoped to one document; (b) `kg_relations.source_session_id` is only the *first* creator (dedup never rewrites it) and relations are shared across documents → deleting "this doc's relations by source_ref" would remove triples other documents also assert (silent knowledge loss). The only genuine residuals are cosmetic `mention_count` inflation and stale relations from a superseded extraction lingering — neither fixable safely without a **per-document KG-provenance table with reference counting**, which is a design item (schema + backfill + retirement logic), not a small guard. Re-file as such if it ever matters; do not attempt the naive delete-by-source.

### Output providers — destructive cleanup (legacy columns DONE; brand shims pending orchestration migration)

**DONE (migration `pc20260617b_drop_outlegacy`):** the three legacy `RoomOutputDevice` columns (`renfield_device_id` / `ha_entity_id` / `dlna_renderer_name`) + the `renfield_device_id` FK + the `renfield_device` relationship were dropped after the prod soak (6/6 rows dual-written, 0 legacy-only). The model + every reader (`OutputRoutingService`, `AudioOutputService`, the `InternalToolService` resolve/dispatch paths, the rooms API) now read ONLY the `(output_provider, output_target_id)` pair; the REST API keeps the legacy field names as input-adapters / computed-response for backward compat. Downgrade re-adds the columns + FK (shape recoverable, data not).

**STILL OPEN — brand shim + discovery-method removal blocked on a missing orchestration migration:** the brand-specific `internal.play_album_on_dlna` / `play_video_on_dlna` / `play_from_server` tools and the per-source discovery methods (`get_available_renfield_devices` / `get_available_ha_media_players` / `get_available_dlna_renderers`) were NOT removed.

- **Shims are NOT dead:** they do Jellyfin/DLNA-server CONTENT orchestration — `play_album_on_dlna` fetches album tracks from Jellyfin and sends a multi-track gapless DLNA queue; `play_video_on_dlna` resolves the visual output + Jellyfin stream URL + video DIDL; `play_from_server` resolves a media-server object via `mcp.dlna.play_from_server`. The generic `internal.play_in_room` plays a SINGLE pre-resolved URL and does none of this. The design's §5 envisioned the AGENT doing the track-fetch/stream-resolution then calling `play_in_room` with the resolved media — that orchestration migration was never built. Removing the shims now would drop album/video/server playback.
- **Discovery methods are NOT dead:** they are the built-in source consumed by `OutputRoutingService.get_aggregated_outputs` (the registry `available-outputs` loop) AND the primary `renfield_devices`/`ha_media_players`/`dlna_renderers` fields of `GET /{room_id}/available-outputs` that the (flag-off) frontend reads.

**TO SHIP the rest:** first build the agent-side media-resolution migration (§5) so `play_in_room` covers album/video/server play, then retire the shims; converge the route + frontend onto the aggregated `output_targets` union so the per-source methods can be folded into the built-in provider adapters. Then patch `config/agent_roles.yaml` (the `media`/smart_home role lists `play_album_on_dlna` + `play_video_on_dlna` at line ~70 — ConfigMap-served `renfield-mcp-config`, NOT in image) to drop the retired tool names.

**SOURCE:** `docs/design/output-providers.md` — eng-review 2026-06-07; cleanup-PR investigation 2026-06-17.

---

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

### Write `docs/STRATEGY.md` — North-Star "WHY circles" doc — IN PROGRESS (skeleton landed 2026-04-30)

**Status (2026-04-30):** Draft skeleton committed in `docs/STRATEGY.md`. 9 `[FOUNDER FILL-IN]` placeholders remain — sections that only Eduard can answer (specific strategic conviction, 5-year ideal, invalidation thresholds). Skeleton is honest about "I want it" being a sufficient solo-founder rationale rather than papering over with rationalization.

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

### ℹ️ Reference (info only, NOT a work item): Meetily — self-hosted call-meeting notetaker

Recorded 2026-07-06 on request — **deliberately no decision taken, no planned
extension; info only.** [Meetily](https://github.com/Zackriya-Solutions/meetily)
(MIT, Tauri: Rust backend + Next.js, ~19k stars, v0.4.x pre-release) captures
system audio + mic locally ("bot-free", works with Teams/Zoom/Meet/any app),
live-transcribes via whisper.cpp/Parakeet (multilingual incl. `de`) and
summarizes via Ollama/OpenAI-compatible endpoints. 100% local, desktop
single-user, no HTTP API.

Relevance: **NO speaker diarization in the open-source Community Edition**
(Pro-only promise, never shipped in CE release notes as of 2026-07) → it does
NOT replace §2 (`docs/design/meeting-transcription.md`); room-meeting
diarization + speaker identity + KB/circles integration stay our build. IF the
online-call capture gap (voice-pipeline-plan.md out-of-scope item) ever gets
prioritized for the work instance, the zero-code path would be: Meetily CE on
the work machine → its exports into a watch folder → existing folder-ingest →
project KB. Also noteworthy as a concept: system-audio capture instead of
meeting-bots.

### Satellite meeting recording ("Renfield, starte Meeting-Aufnahme")

**WHAT:** A satellite (primarily the XVF3800-equipped ones) records long-form meeting audio on voice command and pushes it into the meeting-transcription upload path (`POST /api/meetings/transcribe`). Own phase, deliberately kept OUT of the §2 diarization build (eng-review decision D15, 2026-07-06).

**WHY:** Removes the manual two-step (record on phone → upload). The XVF3800 beamforming array is likely the best microphone in the house for multi-speaker capture.

**PROS:**
- Rounds out the meeting feature into a hands-free product experience
- The diarization spike already produces the decision data (phone-in-table-center vs XVF3800 test recording comparison)

**CONS:**
- A real standalone work package: new WS protocol messages (start/stop/stream long-form audio), Pi-side storage/streaming for hours of audio, LED recording indicator, consent UX (§6 of the meeting plan — recording indicator is legally load-bearing, not cosmetic)

**CONTEXT:** §2 is upload-first; the authoritative design (`docs/design/meeting-transcription.md`) lists this under "Explicitly NOT in §2". Start from the spike's capture-comparison measurements (`bin/run_diarization_eval.py`).

**DEPENDS ON:**
- §2 meeting transcription built (spike gates passed)
- Spike capture comparison showing XVF3800 audio is diarizable

**SOURCE:** /plan-eng-review 2026-07-06 (D15 + Outside-Voice finding "nobody records the meeting")

### ~~Command Center — Phase 3 kiosk mode~~ ✅ SHIPPED 2026-07 · ⚠️ admin board DECOMMISSIONED 2026-07
Origin: 2026-06-27, sparked by the "Apex" (Reznikov Engineering) radial mission-control UI. Design doc + on-brand React prototype landed on `docs/command-center`; cinematic video mockups in `renfield-video/`. **Primary source: `docs/design/command-center.md`.**

> **Update (2026-07):** the kiosk was converted from polling to a `/ws/kiosk` **event-push** hub (plan phases 1a–3), and the **admin `/admin/command-center` board was then DECOMMISSIONED** (phase 4): route/page/`AgentConstellation`/`useCommandCenterModel`/`command_center.py` router + `/api/command-center/*` endpoints removed; the kiosk moved to `components/kiosk/` and is the surviving surface. Below is the original done-record; paths marked `components/command-center/*` are now `components/kiosk/*`. See `tasks/kiosk-active-subsystem-plan.md`.

**Phase 1+2 SHIPPED** (`feature/command-center-phase1`): `/admin/command-center` is routed + live — model composed from `/api/mcp/status` + `/api/tool-health` + `/api/satellites` + `/api/presence/rooms` + `/api/federation/peers` + the NEW read-only ADMIN `/api/command-center/{roles,activity}`. Decaying pulse trail, hover role↔tool reach-edges, drill-downs, activity rail, grouped-list fallback < lg.

**Phase 3 kiosk SHIPPED** (`/kiosk`, `KioskPage.tsx` / `components/command-center/KioskConstellation.tsx` + `useKioskModel.ts`): glow/bloom wall-display variant (deliberately breaks DESIGN.md, sanctioned), voice-reactive core from satellite `state`, reduced-motion-gated alive field, self-hiding weather + now-playing ambient tiles, status colours mirroring the physical satellite LED ring. See CLAUDE.md (Command Center) + `docs/design/command-center.md`. **Nothing remaining** — kept here only as a done record.
- **WHAT:** A single read-first "mission control" page (`/admin/command-center`) that unifies what's today scattered across six admin pages (`/admin/routing`, `tool-health`, `trajectories`, `satellites`, `presence`, `integrations`) into one live radial board: a core (active agent role, live off the chat WS `done` frame) → ring of agent roles → ring of MCP tools coloured by health → ring of satellites/rooms coloured by presence → federation peers. Read-only, every node drills into its existing admin page.
- **WHY:** All the data already exists and is emitted; nothing surfaces it together or *live*. Replaces "open six tabs to understand the system" with one at-a-glance board; reuses role-surfacing's `agent_role` for the live pulse + `presence_map` data for the rooms ring.
- **PROS:** Mostly frontend composition over existing endpoints + the WS pulse — no new inference, no new endpoints, no schema change. Phase-3 payoff (a circle-aware household **kiosk**, "what's the house doing") is genuinely Renfield-unique — no other chat UI has satellites/rooms to show.
- **CONS:** Ring crowding at mobile widths (needs a collapsed/list fallback); the kiosk projection needs real circle-aware authz (a non-admin, content-free view) — that's the costly part, deliberately out of v1. The "stunning"/glowing-orb video aesthetic deliberately BREAKS DESIGN.md and must NOT bleed into the product component (`AgentConstellation.tsx` stays restrained, motion only where motivated — see the cinematic mockups for the marketing-only look).
- **CONTEXT:** Prototype = `src/frontend/src/components/command-center/AgentConstellation.tsx` (+ `types.ts`/`demoData.ts`, `commandCenter.*` i18n), not yet routed. Phase 1 = assemble the live `CommandCenterModel` from the six endpoints + wire the WS pulse + add the `<AdminRoute>` page.
- **DEPENDS ON:** strategic green-light (same premise gate as the chat-UI roadmap — is an ops board / kiosk worth the build for a voice-first household?). Standalone otherwise.

### Self-Learning — gated on burn-in data
Both items below were surfaced by outside voice during the 2026-05-26 `/plan-eng-review` of the admin console PR. Build only when the gate fires.

- **Remove `would_have_injected` shadow log.**
  - **WHAT:** v2.10 ships a shadow query inside `SkillService.find_similar()` that runs the candidate query WITHOUT the `status='approved'` filter and logs the count of candidates that the draft-pool gate filtered out (the recall hit).
  - **WHY:** The gate has zero burn-in evidence — we don't know if the LLM extractor's precision is 90% (gate is a UX tax nobody clears) or 30% (gate is essential). The shadow log makes the precision/recall tradeoff measurable.
  - **PROS:** Drop the dual-query overhead once we have ≥2 weeks of data and a verdict. Simplifies `find_similar` back to one query.
  - **CONS:** None — purely instrumentation removal.
  - **CONTEXT:** Without removal, every find_similar call runs an extra cosine query forever. Negligible per-call cost but adds up.
  - **DEPENDS ON:** ≥2 weeks of post-v2.10 traffic. If precision turns out to be 95%+ and the queue stays empty, the answer might be "remove the gate entirely, not just the shadow log."
  - **TRIGGER:** Owner reviews `would_have_injected` metric ≥14 days after v2.10 deploy and decides on gate's fate.

- **Household cascade-vote approval for shared skills.**
  - **WHAT:** Today the v2.10 PR ships owner-private approval — only the skill's `user_id` (or admin) can approve a draft. But Circles makes retrieval cross-user: A's household-tier skill is retrievable by B via circle reach. There's no mechanism for B to approve A's draft that B would benefit from.
  - **WHY:** If household-tier auto-extracted skills become common, the approval bottleneck on the original owner could starve the whole household.
  - **PROS:** Distributes the curation load. Captures the "this also helps me" signal from circle peers.
  - **CONS:** Voting mechanism = real UI scope (vote UI, vote tallying, quorum rules). Premature without evidence household-tier skills are a real frequency.
  - **CONTEXT:** Outside voice in eng review specifically called this out as an unspecified case. Captured here so it isn't lost.
  - **DEPENDS ON:** Evidence that household-tier auto-extracted skills happen with non-trivial frequency post-v2.10 — measurable via `procedural_skills.circle_tier=2 AND source='auto_extracted'` count.
  - **TRIGGER:** ≥10 household-tier auto-extracted skills observed across the user-base, OR explicit user feedback "I can't get to my partner's drafts."

### ~~Structured-memory reconciler — residual race hardening~~ ✅ FIXED (2026-06-04, `feature/structured-memory-kg`)
Surfaced by `/review` of the branch; all three fixed in the same branch (commit follows the review-fixes commit) with PG tests in `tests/backend/test_kg_reconciler_pg.py`. Kept here as an audit trail.
- **#3 — Concurrent approve of two overlapping proposals.** ✅ `approve_proposal` now closes a no-op merge (survivor `None`, counterpart already tombstoned) as `KG_MERGE_PROPOSAL_SUPERSEDED` instead of a misleading `approved`, and only resolves a still-PENDING proposal. Test: `test_overlapping_approve_marks_superseded`.
- **#4 — `run_reconciler` overlap.** ✅ `run_for_user` wraps each pass in a non-blocking per-user advisory lock (`pg_try_advisory_lock(_RECONCILER_LOCK_NS, user_id)`) on a dedicated connection (`self.db.bind.engine`) so it survives `merge_entities`' mid-pass commits; an overlapping run returns a no-op report. Test: `test_concurrent_run_skips_when_locked`. (Used a session-level lock on a side connection rather than the `pg_advisory_xact_lock` first sketched, because merge_entities commits mid-pass.)
- **#6 — Embedding-null entities never reconciled.** ✅ `backfill_missing_embeddings` re-embeds up to `KG_RECONCILER_EMBED_BACKFILL_PER_RUN` active null-embedding entities at the top of each pass, so they become reconcilable the same run. Test: `test_backfill_embeds_null_entities_then_reconciles`.

### ~~Structured Memory Phase 4 — graph-expansion retrieval~~ ✅ BUILT post-RRF (2026-06-04, `GRAPH_EXPANSION_ENABLED`, dark)
Rebuilt on the **post-RRF single-insertion design** (`docs/HANDOVER_graph_expansion.md` §4) after the per-module MVP was re-deferred by `/plan-eng-review` + outside voice. `services/graph_expansion.py::expand_fused` in `PolymorphicAtomStore.query` (tests: `test_graph_expansion_pg.py` 7/7). All review must-fixes addressed: ✅ post-RRF single seam (no double-work, decay survives); ✅ level-synchronous BFS (correct min-hop, frontier cap can't mislabel); ✅ leak-safe edges (both endpoints accessible, no `name_map` reuse); ✅ per-hop circle filter + per-hop frontier cap; ✅ provenance (`payload.expanded`+`hop`); ✅ anonymous (`asker_id=None`) public-only test. The per-module MVP stays parked on `feature/structured-memory-phase4-subsume` (`2794872`) as a dead end.
**Remaining Phase 4 follow-ups (not blocking; the flag works for the /brain fused path today):**
- **Agent string path:** `get_relevant_context` (the agent's KG string / `internal.knowledge_search`) does NOT yet go through the fused path, so it doesn't benefit from expansion. Handover bullet 3 = refactor it onto the fused path. Until then, expansion enriches `PolymorphicAtomStore.query` consumers only.
- ~~**Pre-existing `name_map` leak (P1):**~~ **FIXED 2026-06-06** (`fix/kg-name-map-circle-leak`). `get_relevant_atoms`/`get_relevant_context` resolved relation endpoint NAMES via an **unfiltered** `name_map` — a visible relation (tier=MIN) could name an owner-only endpoint (into the agent LLM context / the `/wissen` drawer). Both methods now route the endpoint-name lookup through the shared `KGRetrieval._resolve_entity_names()` → `kg_entities_circles_filter` ("?" on miss, mirroring `kg_graph_service.focus`'s per-node gate). Regression test `tests/backend/test_kg_retrieval_name_leak_pg.py` (8 cases, real PG, incl. the cross-user self-tier leak) — green on .159; adjacent suites 21 passed.

### Structured Memory Phase 3-subsume — recall-loss watch (post-enable)
Shipped dark (`MEMORY_SUBSUME_TO_KG`, off). When enabled, `fact`-category memories with a subject are NOT stored flat (they live in the KG). **Risk:** a fact whose object is not a named entity (e.g. "Anna ist müde") may not be captured as a KG relation → lost. Before enabling in prod, validate KG extraction's fact-capture rate on real transcripts; consider a shadow/measure pass first. Trigger: owner wants to reduce flat-memory duplication AND KG fact-capture is validated good.

**Measured (2026-06-16, branch `feat/subsume-recall-guard`):** two-extractor eval (`bin/run_subsume_recall_loss_eval.py` + `tests/eval/subsume_recall_loss_eval.yaml`) against prod models: of 6 subsumed cases, **2 silently lost (67% capture)** — both state/attribute facts ("Tom ist groß", "Oma Erika hat Rückenschmerzen") with `kg_rels=0`. Loss surface confirmed: the memory-extract and KG-extract LLM calls are **uncoordinated** (separate async tasks, separate sessions), so a fact memory labels `fact+subject` while the KG independently emits no relation (object not a named entity → no entity → no relation; also `validate_kg_relation` can reject).

**Harm-reduction guard SHIPPED (default on, `MEMORY_SUBSUME_REQUIRE_KG_RELATION`):** `ConversationMemoryService._subject_is_kg_representable` keeps a fact flat unless the subject's **person**-entity already carries ≥1 relation (subject-level proxy). Protects the never-before-related-subject worst case; **does NOT** stop loss of a state-fact about an already-related person (per-fact loss). Selection mirrors `resolve_entity` (canonical + person-typed + tier/mention ordering) to avoid wrong-entity false-positives. Same-turn relations are intentionally invisible (the race is documented as safe: brand-new-entity-object facts get a harmless flat dup, not loss).

**Per-(subject, turn) gate — DONE 2026-06-16 (`feat/subsume-per-fact-fix`); closes the cross-turn residual, a narrower same-turn residual remains.** Subsume now drops a fact ONLY when THIS turn's KG extraction actually captured a relation for the fact's subject. Coordination design (a): in the **background** path (after the `done` frame, so the turn/TTS/wakeword are never delayed) one ordered coroutine `chat_handler._extract_structured_background` runs the `post_message` hooks FIRST — `kg_post_message_hook` populates a shared `captured_subjects` set with the lowercased subject names of the relations it saved this turn — then runs memory extraction passing that set in as `captured_kg_subjects`. KG extraction runs **exactly once** (the set is reused, never re-extracted). `ConversationMemoryService._should_subsume_fact` makes the per-turn set the PRIMARY gate; `_subject_is_kg_representable` (the old subject-level proxy) is kept only as a fallback for uncoordinated callers (`captured_kg_subjects is None`). Gated by the existing flags: subsume off → coordination off, the two tasks stay independent (legacy, byte-identical); `MEMORY_SUBSUME_REQUIRE_KG_RELATION` off → legacy unguarded subsume.

**Granularity — per-(subject, turn), NOT truly per-fact.** The captured signal is subject NAMES, not (subject, object) pairs. This closes the **cross-turn** residual the proxy missed (proxy keyed on PRIOR relations; this keys on THIS turn's capture). **A narrower residual remains:** a single turn yielding TWO facts about the SAME subject — one with a named-entity object (relation saved → subject in the set) and one a state/attribute fact (no relation) — still subsumes the state fact, because the subject is in the set. The truly-per-fact fix would need a per-(subject, object) captured signal matched to each fact's object (not built; would also need the memory extractor to emit the fact's object).

**Eval (prod models, `bin/run_subsume_recall_loss_eval.py`):** unguarded surface = 3/6 single-fact subsumed lost (50% capture: jutta/tim/tom state-attribute facts, `kg_rels=0`); **`--perfact` single-fact cases = LOST 0** — the 3 danger-zone facts KEPT FLAT, named-entity-object facts still subsumed. The added `mixed-same-subject-de` case ("Anna wohnt in Berlin und ist müde") **documents + measures the remaining same-turn residual**: under `--perfact` the state fact is the loss (`loss_expected: true` for the mixed shape). Tests: `tests/backend/test_memory_subsume_pg.py::TestSubsumePerFactGate` (headline: state-fact about an already-related person kept flat; + same-turn-same-subject mixed case STILL subsumes the state fact) + `tests/backend/test_subsume_coordination.py` (KG-hook capture seam + ordered-coroutine sequencing) + `tests/eval/test_subsume_recall_loss_runner.py::TestClassifyCasePerFact` (incl. the mixed-same-subject residual assertion).

**Still single-user only (caveat UNCHANGED):** does NOT make `MEMORY_SUBSUME_TO_KG` safe for multi-user. The captured set is name-based and the subject comes verbatim from the memory extractor; cross-user subject resolution + tier reach are unaddressed (a fact may be subsumed against a relation the KG captured for a *different* user's same-named entity, and the subject-proxy fallback resolves own-or-unowned entities only). Keep subsume single-user only.

### KG extraction-path embedding conflation (original-bug root) — FIXED 2026-06-05
Root cause confirmed by measurement: entity embeddings = `name + description`, and the
extractor filled `description` with a generic type-floskel ("Vollständiger Name einer Person"),
collapsing every person row to a generic-person centroid that any bare name lands ≥0.85 from
(entity #11 reached 127 mentions, #9 36). Fixed (branch `fix/kg-extraction-person-magnet`,
chosen option = disable-embedding-for-person + prompt + de-magnetize backfill):
- PERSON entities skip the inline embedding-match (gate on multi-type `seed_types`); non-person
  types keep it. `resolve_entity` strips generic descriptions (`is_generic_person_description`,
  whole-string) from person rows before embed/store. Extraction prompt forbids type-meta
  descriptions (all 4 variants). De-magnetize backfill `services/kg_demagnetize.py` +
  `bin/demagnetize_person_entities.py` repairs existing rows. 38/38 on real PG.
- Conflation tripwire (`KG_CONFLATION_MONITOR_ENABLED`, read-only) scoped to NON-person types
  (persons skip embedding-match, names cluster ≥0.85 → flagging is noise). 8/8 PG. Done 2026-06-05.
- `kg_demagnetize --apply` RUN on prod (#9 + #11 NULLed + re-embedded); migration backfill RUN
  (Jutta → own entity #234, not Anna). Done 2026-06-05 (rc.9).
- Reconciler **person-guard** added + `KG_RECONCILER_ENABLED=true` in prod. Done 2026-06-05 (rc.11).
  Person-involving pairs with unrelated names are dropped (no merge/proposal); auto-merge gate
  re-requires name-relatedness for persons (defense in depth). 19/19 PG. See
  [[reference_person_names_embedding_cluster]].
- **Residual follow-ups:**
  - (a) person OCR-variant dedup is now entirely review-gated (the reconciler same-name gate
    doesn't surface differing *spellings* — only normalized-equal names); acceptable per the
    chosen tradeoff but watch document-extraction duplicate persons.
  - (c) ~~the unfiltered `name_map` endpoint-name leak in
    `get_relevant_atoms`/`get_relevant_context`~~ **FIXED 2026-06-06**
    (`fix/kg-name-map-circle-leak`; see the Phase 4 follow-ups section above).

### ~~Paperless PR 5 — Interactive confirm card~~ ✅ SHIPPED #662 (closed in the 2026-06-17 reconciliation sweep)
Shipped via #662 (`2ecffd9` "interactive confirm card replaces typed choice syntax" + `a685836` + review fixes `cdf29b4`); `paperless_commit_tool.py` carries the structured confirm-card path (maps the card's structured decisions). Cold-start threshold made configurable in #661. The gate fired; entry obsolete.

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

### ~~MCPManager Streaming Surface~~ ✅ SHIPPED #406/#407/#412 (closed in the 2026-06-17 reconciliation sweep)
Shipped via the v2-federation chain: `execute_tool_streaming(...) -> AsyncIterator[ProgressChunk | FinalResult]` exists in `services/mcp_client.py` (with `progress_sink`, `_execute_tool_streaming_impl`, and the locked chunk vocabulary in `services/mcp_streaming.PROGRESS_LABELS`). #406 (`e98e372` "streaming surface — F1 of v2 federation") + #407 + #412. The exact API this item requested is built.

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

### Speaker-profile + wakeword-template in-memory cache (P3, measure first)

**WHAT:** Cache enrolled speaker centroids and (user, keyword) wakeword templates in process memory (invalidated on enroll/promote/delete/merge) instead of a DB load per voice turn.

**WHY:** `speaker_resolver.resolve_speaker_from_embedding` loads all known speakers on every turn, and A1 wakeword verification adds a template load per wakeword detection. At 3-person household scale this is milliseconds — an optimization, not a flaw — but it becomes measurable if profile counts or turn rates grow.

**CONTEXT:** Flagged in the 2026-07-06 `/plan-eng-review` of `docs/design/voice-identity-wakeword-verification.md` (Section 4, perf). Deliberately NOT built with A1 per the measure-first rule. Invalidation hooks belong in `SpeakerEnrollmentService` mutations (enroll/promote/dismiss) and the speaker delete/merge paths fixed in `pc20260705`.

**DEPENDS ON:** A1 (wakeword verification) having shipped — else only the resolver path exists; and a measured per-turn cost worth removing.

**SOURCE:** `docs/design/voice-identity-wakeword-verification.md` eng review D15

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
