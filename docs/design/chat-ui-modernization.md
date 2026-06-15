# Chat UI Modernization

Status: **Design / backlog** — survey + prioritized roadmap, not yet scheduled.
Scope: the `/chat` surface (`src/frontend/src/pages/ChatPage/`). Does not touch
voice/satellite transport, agent routing, or circles enforcement — those are the
moat this work is meant to *expose better*, not replace.

## Why this doc exists

The chat interface has drifted behind the locally-hostable LLM-UI field. This doc
surveys what the leading self-hosted chat UIs do, identifies the concrete gaps,
and proposes a priority order that plays to Renfield's strengths (voice,
satellites, circles, smart-home, fully offline) instead of chasing parity for its
own sake.

The guiding principle: **adopt the interaction patterns that are now table-stakes,
skip the ones that fight Renfield's architecture, and invest the saved effort in
the things no other chat UI can do because they don't have satellites or a
household trust model.**

## Prerequisite — validate the premise (do this BEFORE scheduling)

This roadmap assumes the web `/chat` surface is worth heavy investment. Renfield
is a **voice-first household assistant**; a large share of real interactions may
be voice/satellite, not the web text chat. That assumption is currently
**unmeasured**, and it gates everything below.

Before committing any tier:

1. **Instrument interaction share** — web-chat turns vs voice/satellite turns,
   per household member. If the web chat is a minority surface, most of this
   roadmap is polishing the wrong thing.
2. **Establish an a11y + mobile baseline** — Renfield is a PWA and `CLAUDE.md`
   mandates dark-mode + i18n per component; accessibility and touch are
   first-order, not Tier 3 polish (see Tier 0 below).

**The tier/sequencing order below is PROVISIONAL until that data exists.** Treat
it as "what to build *if* the web chat proves high-value," not a commitment.

## Survey: locally-hostable chat UIs

| Project | Stack | Strengths worth stealing | Not relevant to us |
|---|---|---|---|
| **Open WebUI** | Svelte + FastAPI | Model/persona switcher, per-message regenerate + branch, RAG document chips, prompt library, command palette (`/` commands), markdown+code+LaTeX rendering, message search | Its own auth/RBAC (we have circles), Ollama model mgmt (we route per-role) |
| **LibreChat** | React + Express | Conversation **branching/forking**, message edit-and-resubmit, preset "agents", multi-model side-by-side, conversation search, import/export | Plugin marketplace, paid-API key vaults |
| **Lobe Chat** | Next.js | **Artifacts/canvas** (render generated HTML/SVG/React inline), plugin "tools" with rich UI, persona "agents" market, TTS/STT toggles, mobile PWA polish | Cloud sync, marketplace economy |
| **Hugging Face Chat-UI** | SvelteKit | Clean streaming, web-search tool surfacing, share-conversation links | Tied to TGI, no household model |
| **text-generation-webui** | Gradio | Raw model controls, character/persona cards, "lorebook" context injection | Power-user sprawl, not a household UX |
| **AnythingLLM** | React | Workspace-scoped RAG, citation surfacing, embeddable widget | Workspace model overlaps our circles/KB |

Cross-cutting patterns that have become **table-stakes** in 2025-era self-hosted
chat UIs and that Renfield currently lacks or under-delivers:

1. Conversation **branching** (fork from any message, edit-and-resubmit).
2. **Follow-up suggestion chips** under the last assistant turn.
3. In-conversation and cross-conversation **message search**.
4. **Command palette** / slash-commands for actions and navigation.
5. **Artifacts / canvas** — render generated structured output (tables, SVG,
   small HTML/widgets) inline instead of as a code block.
6. **Persona / role surfacing** — let the user see and steer which agent role is
   answering.
7. **Citation / provenance chips** on RAG-backed answers.

## Where Renfield already stands

Renfield's chat is not behind on the things that matter most for a *voice-first
household assistant*, and it has capabilities none of the survey projects do:

- **Voice + barge-in + wake-word** end to end (`ChatContext`, `useChatWebSocket`,
  `wakeWordRecovery.ts`, `AudioVisualizer.tsx`) — a first-class modality, not a
  bolt-on TTS toggle.
- **Satellite room awareness** — the same conversation can be spoken in a room and
  followed across rooms (Media Follow Me, presence). No survey project has a
  physical-room model.
- **Circles (access tiers)** — every retrieval is trust-gated per household member.
  The survey projects have flat per-user RBAC at best.
- **Real server-side tool execution** — Paperless forwarding, folder ingest,
  smart-home, BT scan, presence — surfaced today as cards
  (`PaperlessConfirmCard.tsx`, `AttachmentQuickActions.tsx`, `EmailForwardDialog.tsx`).
- **Per-role agent routing** already exists (`agent_roles.yaml`); the UI just
  doesn't expose it.

So the work is **not** "catch up to Open WebUI." It is "adopt the half-dozen
interaction patterns that are now expected, and surface the platform power we
already have but hide."

## Gap analysis → recommendations

Each item notes the primary files it would touch, so this doc is actionable.

### Tier 0 — cross-cutting / table-stakes (gates Tier 1)

These are not features; they are baseline qualities every Tier 1+ item must
satisfy. The survey-driven item list below omitted them, which is exactly the
trap of optimizing for "what Open WebUI has." None of these should be deferred.

- **Accessibility (a11y).** WCAG basics: full keyboard navigation, ARIA roles,
  screen-reader labels, focus management. **Hover-only per-message actions
  (item 1) are an a11y anti-pattern** — they need a keyboard/focus equivalent.
  `CLAUDE.md` already mandates dark-mode + i18n per component; a11y belongs in
  the same tier of non-negotiable.
- **Mobile / touch / PWA.** Renfield is a PWA and the kitchen-phone form factor
  may be the *primary* one. A `/`-key palette, hover actions, and side-by-side
  affordances are desktop-only as specified — every Tier 1 item needs a touch
  story.
- **Voice-transcript UX.** The product calls voice first-class, yet no item
  covers how STT transcripts render in the thread, partial/streaming transcript
  display, editing a mis-heard transcript, or the barge-in visual state.
- **Stop-generation + error/retry.** Table-stakes in every surveyed UI and
  currently unlisted: interrupt a running generation; clear error + retry on a
  failed turn.
- **Offline / resource-degradation UX.** "Fully offline" is the premise. Define
  what chat shows when Ollama is busy/down, the shared GPU is saturated by
  another household member, a satellite drops, or a tool MCP is unreachable —
  for a shared-GPU household this is a routine state, not an edge case.

### Tier 1 — high value, fits the architecture cleanly

1. **Conversation branching (fork + edit-and-resubmit).**
   The single biggest expectation gap. Let a user edit any prior user message or
   regenerate any assistant turn, creating a branch instead of mutating history.
   - Backend: conversation persistence already stores message rows; add a
     `parent_message_id` / branch pointer and a "fork from message" endpoint.
   - Frontend: `ChatMessages.tsx` per-message hover actions (edit / regenerate /
     branch); a branch switcher in `ChatHeader.tsx`.
   - **Design risks (resolve when scheduled, not now):**
     (a) *Memory extraction* — `ConversationMemoryService` extracts memories from
     messages; a fork must pick a canonical branch and not double-extract.
     (b) *conv_context re-injection* — the agent re-injects history with the
     `[VORHERIGE_FEHLGESCHLAGENE_AKTION]` stale-error marker; only the active
     branch should be replayed, and a dead branch's failed-action markers must
     not leak into the next turn.
   - Risk: medium — touches conversation persistence schema (`Message` is
     integer-keyed; add a `parent_message_id` self-FK). Migration required.

2. **Follow-up suggestion chips.**
   After an assistant turn, show 2-4 tappable follow-ups. High impact for a
   voice/household UX (kids, hands-busy cooking).
   - **Local-LLM perf constraint:** the chips MUST come from the *same*
     generation (a structured trailing block / one structured-output call), NOT
     a second Ollama call. A separate inference per turn adds latency to *every*
     turn and competes for the shared household GPU. Make them async/best-effort
     so they never block the answer.
   - **Hidden cost:** "same generation" is not free — it changes the agent
     **output contract across every role** in `agent_roles.yaml` and leans on
     reliable structured-output parsing from small local models, which the rest
     of the codebase explicitly distrusts (cf. the `play_radio` guard, the KG
     conflation guards). Budget prompt + parser work, not just a frontend chip.
   - Frontend: render under the last turn in `ChatMessages.tsx`; tapping fills
     `ChatInput.tsx`. Backend: optional `suggested_followups` field on the final
     stream frame. For voice, the chips should also be **speakable** (the
     hands-busy use case the item is justified by) — not tap-only.
   - Risk: low-medium — no schema change, but touches the agent output contract.

3. **Message search (in-conversation + global).**
   - Backend: FTS over the `Message` table. `Message` (`models/database.py`) has
     **no** `search_vector` column today, so this needs an added STORED
     `tsvector` column + migration + a backfill for existing rows — reuse the
     existing pattern on `document_chunks` / `conversation_memories`, not a new
     mechanism.
   - **Scoping:** `Message` is **not an atom** — it has no `circle_tier`/`atom_id`.
     Do NOT route it through `circle_sql.py`. Scope message search by
     **conversation ownership** (and household-shared conversations if/when that
     exists). `circle_sql` stays the rule for *atom-bearing* reads (provenance
     #7, KB), which messages are not.
   - Frontend: a search field in `ChatSidebar.tsx` + jump-to-message.
   - Risk: medium — schema change (the `tsvector` column + backfill).

4. **Command palette / slash-commands.**
   `/` in `ChatInput.tsx` opens an action+navigation palette: jump to a page,
   start a routine, switch agent role, attach from a watch-folder, scan BT, etc.
   Maps onto existing internal tools and routes — a discoverability win.
   - **Not purely client-side.** A palette that starts routines / switches roles
     / scans BT invokes *privileged* server tools, so it needs per-user circles
     authz on **both** which actions are *displayed* and which can *execute* —
     reuse the same permission gating as the rest of the agent tools. The
     client-only part is the UI; the gating is real backend work.
   - Risk: medium — UI is light, but per-user action authz is required.

### Tier 2 — high value, more build

5. **Artifacts / canvas.**
   Render generated tables/SVG/small HTML inline (a shopping list, a weekly plan,
   a chart) instead of a fenced code block. This pairs naturally with the
   `conversation`/writing role and with smart-home status summaries.
   - Frontend: an artifact renderer component + a safe (sandboxed) HTML/SVG path.
   - Backend: mark a message part as `artifact` with a type.
   - Risk: medium-high — needs a strict sanitization boundary (treat model output
     as untrusted; no raw `dangerouslySetInnerHTML` without a sandbox).

6. **Agent-role surfacing + steer.**
   Show which role answered (smart_home / media / documents / presence / general)
   as a small badge, and let the user pin a role for the next turn. The router
   already picks one (`agent_router.py` → `agent_roles.yaml`); this makes it
   visible and correctable when it mis-routes.
   - **Note — the agent role is NOT already on the wire.** `Message.role`
     (`models/database.py`) is the *chat* role (`user`/`assistant`), a different
     concept. The resolved *agent* role is computed in `agent_router.py` at
     request time; it must be **plumbed onto the WS stream** (final frame) and,
     to show it on *historical* turns, **persisted in `message_metadata`**.
   - Frontend: a role badge in the assistant turn; a role pin in `ChatHeader.tsx`.
   - Backend: emit the resolved role on the stream + optional persistence.
   - Risk: medium (backend stream plumbing + optional persistence, not UI-only).

7. **Provenance / citation chips.**
   RAG and document-fact answers should carry source chips (which document /
   which KB / which tier) — Renfield already has the atom + tier metadata
   (`/api/atoms`, `FactProvenance`), so this is surfacing, not new retrieval.
   - Risk: low — data exists.

### Tier 3 — Renfield-unique, no survey project can copy these

8. **Room-aware conversation handoff in the UI.**
   Show "continued in Wohnzimmer" / "now playing in Küche" inline when a
   conversation or media follows the user across rooms (presence + Media Follow).
   This is the payoff of having satellites — make it visible.

9. **Household-shared vs private conversation surfacing.**
   Circles already gate retrieval; the chat list could show which conversations
   are visible to which tier, with a tier badge (reuse `TierBadge`). Lets a
   household member understand "who could see this exchange" at a glance.

10. **Generative-UI widgets for smart-home / status.**
    When the answer is a device state or a routine result, render a live widget
    (toggles, a thermostat dial, a presence map) instead of prose. Builds on the
    artifact boundary (item 5) plus the existing HA glue.

## Design decisions to resolve per item (when scheduled)

From `/plan-design-review`. These are the design decisions each item will
silently default at implementation time if not specified — every item's own
`/plan-design-review` (with mockups, against `DESIGN.md`) must answer them
before it's built. The recurring miss across the roadmap is **interaction
states**: an item is not designed until its empty / loading / error / partial
states are.

| Item | States that must be specified | Placement / hierarchy | a11y + touch |
|---|---|---|---|
| 1 Branching | switching state; "this is a forked branch" affordance; what a deleted/empty branch shows | branch switcher in `ChatHeader.tsx`; per-message actions must not crowd the turn | actions need a keyboard/focus path (hover-only fails a11y); switcher reachable on 375px |
| 2 Follow-up chips | **none generated → render nothing, not an empty container**; long-answer overflow (wrap vs scroll vs cap at N); chips while still streaming | under the last assistant turn; must not push the composer off-screen on mobile | 44px touch targets; keyboard-focusable; **speakable** for the hands-busy case |
| 3 Message search | searching/loading; **zero results (warm empty state, not "No results")**; no-match vs no-permission | search field in `ChatSidebar.tsx`; result → jump-to-message scroll-into-view | keyboard nav of results; focus return after jump |
| 4 Command palette | empty query (recent/suggested actions); zero matches; **per-user: hide actions the user can't run** (circles authz) | overlay/modal; does not obscure the active turn | full keyboard nav (arrow/enter/esc); the `/` trigger needs a touch equivalent (no keyboard on mobile) |
| 5 Artifacts / canvas | loading; **render-fail / sandbox-blocked fallback**; partial/streaming artifact | inline in the turn, clearly bounded as generated content | sandboxed content must still pass contrast/zoom; focus trap inside interactive artifacts |
| 6 Role badge | unknown/unrouted role; role-pin active vs cleared | small, in the assistant turn header; must read as metadata, not a CTA | not color-only (a11y) — pair the tier/role color with a label/icon |
| 7 Provenance chips | **no source → show nothing**; many sources (cap + "more"); cross-tier source | attached to the answer, below the turn | chips are links → keyboard-focusable, visited-state per universal rules |
| 8-10 (Tier 3) | room-handoff transient state; shared-vs-private affordance; live device-widget loading/stale/unreachable | inline, reuse `TierBadge`; widgets must degrade to text | reuse the same a11y baseline; widget controls are real controls (labels, focus) |

All new components (chips, palette, branch switcher, artifact renderer, role
badge) must either map to an existing `DESIGN.md` token/component or be added to
the design system's vocabulary — not invented ad hoc. Calibrate against
`DESIGN.md` at build time; the AI-slop blacklist applies (no generic chip rows,
no icon-in-circle decoration).

## Explicitly out of scope / skip

- **Model marketplace / persona economy** — Renfield routes per-role locally;
  a persona market fights that model.
- **Cloud sync / share-to-web links** — violates the offline/self-hosted premise.
- **Its own auth/RBAC** — circles already own household trust; do not bolt on a
  second permission system.
- **Lorebook/character-card sprawl** (text-generation-webui style) — power-user
  surface that doesn't fit a household assistant.

## Suggested sequencing

**Gated on the premise check above** — this ordering only holds if the web chat
proves to be a high-value surface. It also assumes **Tier 0 (a11y, mobile,
voice-transcript, stop-gen, offline UX) lands alongside or ahead of Tier 1** —
those are not deferrable.

Note this is ordered by *value*, not by implementation cheapness. The earlier
"no-migration, client-only" framing was a cost criterion masquerading as a value
one — corrected here:

- **First slice:** **(2) follow-up chips → (4) command palette → (7) provenance
  chips.** Provenance (7) ranks high because it surfaces the circles
  trust-transparency moat, not just because it's cheap.
- **(6) role surfacing is NOT in the cheap slice** — it needs backend stream
  plumbing + `message_metadata` persistence (re-graded medium). Sequence it with
  the structural work, not the quick wins.
- **Caveats that move real cost earlier than the labels suggest:** (2) is only a
  "frontend chip" if chips ride the same generation — making that reliable
  touches the agent output contract across every role and leans on local-model
  structured-output fidelity the rest of the codebase distrusts. (4) is not
  "client-side": a palette that starts routines / switches roles / scans BT
  invokes privileged tools and needs per-user circles authz on both *display*
  and *execution*.

Then the structural work: **(1) branching** (persistence migration + the
memory/conv_context design risks), **(3) message search** (tsvector column +
backfill), and **(6) role surfacing**.

Tier 3 items are the differentiators and should follow once the artifact
boundary (5) exists, since 8/9/10 reuse it.

## Non-negotiables for any of this

- Every new **atom-bearing** read path (provenance #7, KB-backed surfacing,
  shared-conversation reads over atoms) goes through `services/circle_sql.py` —
  no second unfiltered path, same rule as the rest of the corpus. **Message
  search (#3) is the exception:** messages are not atoms, so it scopes by
  conversation ownership instead — do not force it through the atom circle
  filter.
- Model output rendered as an artifact is **untrusted**: sandbox it. No raw HTML
  injection.
- Dark mode (`dark:` variants) and i18n (`useTranslation`, both `de.json` +
  `en.json`) on every new string and component, per `CLAUDE.md` / `DESIGN.md`.
- `DESIGN.md` tokens only — new component classes follow the existing
  `.card` / `.btn-*` / `.tier-badge` conventions.
