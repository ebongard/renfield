# GUI Contribution Model — how features (esp. MCP-backed ones) plug in UI

**Status:** Design / proposal (not yet built). Motivated by the Simba per-instance-optional analysis (#1170/#1178, 2026-08-31).
**Author:** design pass, 2026-08-31 (rewritten after a three-lens eng/UI/design review the same day — see §11).
**Scope:** how a new capability — usually an MCP server — contributes GUI (a menu action, a review queue, a status icon, a widget, a settings panel) **without** bloating the shared frontend or requiring an N-file edit of the shell, and such that an instance **without** that capability carries **zero** of its UI.
**Design authority:** `DESIGN.md` at the repo root is the binding constraint on everything in this doc — tokens, spacing, motion, semantic colors, the tier visual language. Any renderer or component introduced under this model must pass `/plan-design-review` + `/review` against it (see §7).

---

## 1. Problem

Renfield ships **one** frontend image across all instances (household, xidra, any future one); per-instance behaviour comes from runtime config (`GET /api/config/features`, `chat_starters`, etc.). Today, when a capability needs UI it is hand-wired into the shared React app in several places and gated by a feature flag:

- a review section slotted into `pages/BrainReviewPage.tsx` (e.g. `SimbaIngestReviewSection`, `DocumentDuplicatesSection`, `PdfSplitReviewSection` — each imported and rendered with an `enabled={features?.…}` prop; `MergeProposalsSection` is the odd one out, rendered bare and self-hiding via `return null`);
- an action in `pages/ChatPage/AttachmentQuickActions.tsx`;
- a row action + status icon in `pages/KnowledgePage.tsx` (`Archive`=Paperless, `Landmark`=Simba, the latter also gated on `simba_ingest_review_enabled`);
- feature-flag plumbing in `api/resources/brain.ts` → `api/routes/config.py`;
- i18n keys in `i18n/locales/{de,en,it}.json`.

This works, but has three costs, made concrete by the **Simba** case:

1. **Bundle/footprint residue.** The shared image ships every feature's bespoke components even on instances that don't use them (Simba UI ships to household, flag-hidden). Not visible or behavioural, but it *is* "carried."
2. **Friction.** Adding UI for one capability means editing 5+ shell files + a flag + three locale files, coupling the capability's cadence to the shell's.
3. **Leak risk.** Anything registered unconditionally leaks. The Simba internal agent tools were registered for *every* instance and surfaced via the `general` role (`internal_tools: null` = all tools) even where Simba wasn't configured — fixed in #1178 by gating registration on MCP presence (the `simba_available` guard in `services/agent_tools.py`). UI has the same failure mode if not gated.

**Requirement (from the operator):** keep Simba on xidra, but a Simba-less instance must carry **no** Simba artifacts. Generalised: *a capability's UI should be present exactly where the capability is, and nowhere else — ideally without shipping its code at all where it's absent.*

---

## 2. Options considered

### Option A — Micro-frontends (module federation / per-feature bundles / iframes / web components)

Each capability ships its own frontend bundle, loaded at runtime by a shell.

**Rejected for Renfield.** It fights everything the app is:

- **Tight UI integration.** One design system (`DESIGN.md` tokens), one TanStack Query cache (`api/keys.ts`), one WebSocket, one auth + circle-tier context, dark mode, i18n. Remotes must share React as a singleton and reach that context, or duplicate it — the coupling where module-federation setups rot (shell↔remote version skew).
- **Offline + strict CSP + self-hosted posture.** `src/frontend/nginx.conf` enforces a locked-down CSP (`default-src 'self'; … connect-src 'self'; object-src 'none'`, applied `always` on every location) — a **runtime** remote-bundle fetch/import is structurally blocked. This cuts against the whole offline-capable, self-contained model.
- **Scale mismatch.** Module federation earns its complexity at many-team org scale. Renfield is one codebase with a handful of optional features; the operational + cognitive cost dwarfs the benefit.
- Iframes: strong isolation, poor integration (styling/auth/messaging). Web components: lighter, but still can't share React context (auth/query/i18n) cleanly.

**Honest caveat (not a reason to revisit):** the CSP objection is specifically about *runtime* remote bundles. *Build-time* per-feature code-splitting (bundles built together, tree-shaken, no runtime fetch) is the same family as Option D below — it's the legitimate slice of "federation," and we keep it as the last-resort escape hatch, not the default.

### Option B — Contribution registry inside the existing app (declarative extension points)

Not separate bundles. The shell declares a fixed set of **extension points (slots)**; a feature *registers* its contributions (real React components); the shell renders them, gated by feature flags. The code still ships in the one bundle.

This is what Renfield already does ad-hoc (the review sections composed in `BrainReviewPage`, the attachment-menu actions, the doc-row actions/status icons). Formalising it turns "edit 5 files" into "register a contribution" — while keeping full-fidelity bespoke React for anything interactive.

### Option C — Server-driven UI (SDUI) via typed artifacts

The backend/MCP emits **typed JSON** (`{kind, data}`); the shell renders it with a *fixed* set of React components dispatched by `kind`. Already shipped as **Lane A typed artifacts**:

- Frontend: `components/chat/artifacts/ArtifactRenderer.tsx` + `artifactSchema.ts` (zod discriminated union) render `table` / `list` / `keyvalue` / `chart` / `weather` / `device_control` / `presence_map`; fail-closed to an **escaped code block** on unknown/invalid shape.
- Backend: `services/artifact_service.py` (kind allowlist + size/row/series/point caps — a **DoS gate, not a design gate**) validates; `services/widget_tools.py` lets the agent emit typed widgets; `AdaptiveCardRenderer.tsx` is the sibling card path.

**What SDUI actually buys, precisely (the review corrected the original framing here):**

- *Reusing an existing kind* ships **zero new frontend code** — a feature emits `table`/`list`/… JSON and the generic renderer handles it. This is real and valuable.
- *Adding a new kind* is **not** free: it is a shell edit (a new `case` in the `ArtifactBody` switch, an import, a new `ARTIFACT_KINDS` entry, a new zod branch in the discriminated union, a new sub-renderer component, plus the backend allowlist entry) — and that code ships to **all** instances, because the renderer is generic and static.
- The dispatched sub-renderers are **not** magically generic: `DeviceControlArtifact.tsx` is ~270 lines of hand-written stateful React (optimistic update, per-entity debounce, unmount cleanup, revert-on-failure, `role="switch"`, 44px targets). SDUI does not eliminate bespoke React for interactive kinds — it just changes *where the dispatch happens*.

So the honest per-instance-clean property is: **artifact *data* is per-instance-clean (no data ⇒ no UI); a new kind's *code* is not — it ships everywhere unless removed by the build-time flag (Option D).** SDUI-clean only truly holds when a feature reuses existing read-only kinds.

**Limits (decisive for the default, see §3):** interactive/stateful/durable UI — the Simba two-step claim-before-act confirm, the branch `‹ n/m ›` switcher, the dedupe survivor-radio + supersede/delete + 5s undo, the 3D Wissensgraph — can't be pure SDUI without dragging bespoke state and validation back in, and SDUI's data-owns-text model breaks the repo's i18n mandate for feature UI (§8).

### Option D — Build-time feature tree-shaking (`VITE_FEATURE_*`)

A build arg per capability; unused slot contributions are tree-shaken out of an instance's bundle. This is the **only** option that removes feature *code* (not just data/UI) per-instance. It diverges the "one shared image" model (per-instance builds), so it's a deliberate trade reserved for a real bundle-size/compliance need — not a default.

---

## 3. Recommendation — registry-first; SDUI for read-only agent content

Build **B as the default for feature UI**, use **C for what it's genuinely good at**, keep **D as the last resort**, and do **not** build A.

1. **The contribution registry (Option B) is the default way a capability contributes *feature* UI** — anything durable, actionable, stateful, or risk-bearing (review queues, settings, upload/confirm flows, nav, doc-row actions). Features register bespoke React into declared slots instead of editing the shell. This delivers the "register, don't edit 5 files" win at **full interaction and design fidelity**, and is a near-pure refactor of what already exists.
2. **Server-driven typed artifacts (Option C) are the default for *agent-generated, read-only, data-shaped, in-conversation* output** — `table`/`list`/`keyvalue`/`chart`/`weather`/`presence_map`. These already ship; keep pushing them. Investment = widen the **read-only** artifact vocabulary as needed, under the design-compliance invariant in §7.
3. **Every contribution is capability-gated** by the same signal that gates the backend (a feature flag from `/api/config/features`, itself often derived from MCP presence — the UI analogue of the #1178 `simba_available` tool-registration gate).
4. **Build-time tree-shaking (Option D) is the last resort**, only if bundle purity itself becomes a hard requirement.

The dividing line between B and C is not a per-PR judgement call — it's a checklist (§4).

> **Why this inverts the first draft.** The original recommendation made SDUI the *default* GUI-contribution path and claimed it absorbs "~80%" of feature UI with "zero frontend code, per-instance-clean for free." The review (§11) showed: a new kind *is* a shell edit shipped to every instance; the generic `form`/`review_card` premise collapses on the first stateful feature (Simba, dedupe); SDUI's server-owned text violates the i18n mandate for feature UI; and the existing renderers already drift off-token. The "~80%" figure had no evidence and is dropped. Registry-first keeps the genuine win (pluggable, per-instance-gated, low-friction) without those costs.

---

## 4. When artifact vs bespoke slot — the hard checklist

A surface **MUST be a bespoke slot component (Option B)** if **any** of these hold:

- it is **durable/actionable** (a review queue, settings panel, upload flow) rather than an ephemeral answer in a chat turn;
- it carries an **irreversible or destructive** action (Simba upload, delete, merge);
- it needs **undo**, **staged confirm**, **optimistic reconcile**, or **dependent/reactive fields** (category→type, survivor→supersede);
- it needs **cross-surface deep-linking** (a stable DOM anchor on a routed page — the `#frist-{id}` ↔ `?doc={id}#fakten` ↔ `#simba-{id}` web);
- it needs **risk-aware focus/emphasis** (e.g. autofocus Reject + de-emphasize the destructive control on a cross-tier merge);
- it must survive **offline / history-reload as a *live* control** (not a frozen snapshot);
- it renders **tier / provenance / legal-gate** visual language (see §7).

A surface **defaults to a read-only artifact (Option C)** only when it is **all** of: agent-generated (or trivially data-derived), read-only, data-shaped, and acceptable as a point-in-time snapshot inside a chat turn — i.e. `table`/`list`/`keyvalue`/`chart`/`weather`/`presence_map`.

**Consequences for the proposed new kinds:**
- **`form`** — allowed only for *trivial, stateless* pickers whose submit is a single safe write-back frame (the `device_action` precedent). It may **not** carry staged confirm, response-driven reshape, or dependent-field logic — those pull the interaction back to a bespoke slot. Simba's picker + irreversible two-step confirm therefore stays a bespoke slot; a `form` could at most replace the trivial field entry, not the claim-before-act flow, so it earns little here.
- **`review_card`** — **not adopted.** The three flows it would generalize (`MergeProposalCard`, `DocumentDuplicateCard`, `SimbaProposalForm`) each carry undo, conditional emphasis, staged confirm, or response-driven reshape that JSON can't express without becoming a worse, stringly-typed React. Genericize them instead via a **shared bespoke React component + the slot registry** (§5) — that delivers the "register, don't edit 5 files" win without the SDUI tax.
- **`status_badge` / `row_action`** — as **slot contributions**, not artifacts (they belong on routed list/table rows, and must reuse the existing tier/status primitives; §7).

---

## 5. Extension-point taxonomy (the slots for Option B)

A declared registry (`features/contributions.ts` or similar) exposing named slots the shell renders. Initial set, drawn from what already exists ad-hoc:

| Slot | Shell host today | Contribution shape |
|---|---|---|
| `nav.item` | `components/Layout.tsx` | label + icon + route + permission + `feature` flag |
| `review.section` | `pages/BrainReviewPage.tsx` | a component + `enabled` flag (Simba / dedupe / pdf-split already fit) |
| `attachment.action` | `pages/ChatPage/AttachmentQuickActions.tsx` | label + icon + handler + `enabled` flag |
| `documentRow.action` | `pages/KnowledgePage.tsx` | icon + handler + `enabled` flag |
| `documentRow.statusIcon` | `pages/KnowledgePage.tsx` | icon + predicate over the row (`in_paperless`, `in_simba`) + flag |
| `settings.panel` | settings pages | a component + flag |
| `dashboard.widget` / kiosk | kiosk / dashboard | a component + flag |

Each contribution carries its `feature` flag; the shell renders it only when the flag (from `useFeatureFlags()`) is on. A capability that's absent registers nothing effective, so nothing renders — the leak class of #1178 becomes structurally impossible for UI. Slot components are ordinary React, so they keep full design + interaction + i18n fidelity and their existing RTL/component tests.

**Migration note (not a "pure refactor"):** the current sections don't share one contract — `MergeProposalsSection` self-gates (`return null`) while the others take an `enabled` prop, and each owns its own undo-toast timing, per-section query invalidation, and permission gating. Normalizing them onto one slot shape is a real refactor with regression surface; it must preserve each section's state behaviour, not just its markup.

## 6. Read-only artifact vocabulary (for Option C)

Widen the artifact vocabulary only for **read-only, data-shaped** surfaces, each validated JSON → generic React under the §7 invariant, same fail-closed contract:

- **`key_value` / `table` / `list` / `chart` / `weather` / `presence_map`** already exist.
- Additional read-only kinds (e.g. a timeline, a stat grid) are welcome under the same rules.

Interactive kinds are deliberately **out of scope** for the vocabulary (see §4). The one existing interactive kind, `device_control`, stands as the *maximum* interactivity SDUI should carry — a cheap, idempotent, optimistically-paintable action with a fail-closed permission-gated write-back — and even it is a bespoke stateful component dispatched by the renderer, not "generic."

**Write-back, if ever extended:** the only sanctioned artifact→action channel is the `device_control` → `device_action` WS frame, intercepted pre-validation and **fail-closed permission-gated** in `chat_handler` (denies unless the user holds `HA_CONTROL`) before dispatch to `internal.device_action`. Any future interactive artifact must reuse that pattern *and* declare an explicit loading/pending + stale/offline/disabled state in its schema — because an artifact rehydrated from cache or after a WS drop otherwise presents live-looking controls on a dead socket. This is a strong reason to keep interaction in slots, where TanStack Query already owns loading/empty/error/refetch.

## 7. Design compliance (the guardrail that makes SDUI safe)

`DESIGN.md` is the binding source of truth. SDUI moves *what renders* to runtime data, which removes the diff that `/review` uses to catch off-token colors — so the model is safe **only** with an explicit enforcement seam. The existing renderers prove the risk: `ChartArtifact.tsx` hardcodes hex literals (violating DESIGN.md's absolute "never hardcode hex" rule), `DeviceControlArtifact.tsx` uses off-palette `amber-500`, `ArtifactShell` hand-rolls a box instead of the `.card` utility, and **no artifact uses any tier/provenance primitive** (`TierBadge`, `.tier-badge-{0..4}`, `FactProvenance` are all unused). Remediating those is a prerequisite to scaling this code up.

Two invariants make SDUI a design-system *strength* (one reviewed renderer instead of N drifting components) rather than a drift multiplier:

1. **SDUI data is style-free.** `{kind, data}` carries **content + declared semantics only** — never color, hex, spacing, font, class name, or motion. Semantics are declared with typed fields the renderer maps to tokens: `tier: 0..4`, `severity: success|info|warning|error` (→ the 2-axis warm/cool semantic colors), `provenance: deterministic|advisory`, `legalGate: bool`. Without these fields the tier/semantic language flattens to neutral gray (as today's artifacts do).
2. **The renderers are the single, mandatory design chokepoint.** They — and every new kind — are the only place tokens appear at runtime, so they must pass `/plan-design-review` + `/review` against `DESIGN.md` before shipping. Add a **lint/test gate** over `components/chat/artifacts/**`: forbid hardcoded hex and off-palette Tailwind hues (`amber`, `indigo`, …); require `.card`/token/tier-primitive usage. The current `ChartArtifact` hex and `DeviceControl` amber would fail it.

**Design-system-aware primitives.** Where a read-only kind touches the tier/provenance/status language, it must render *through* the existing elements, not reinvent them: tiers → `TierBadge` + `.tier-badge-{0..4}`; provenance → `FactProvenance` (✓ deterministic / ~ advisory); status → the existing status-icon set. Any slot component (Option B) inherits these directly.

**Motion.** All artifact motion is fixed in the renderer, drawn only from `DESIGN.md` §Motion tokens, `prefers-reduced-motion`-gated, and **not** specifiable by data — consistent with the "motion must be motivated" rule.

## 8. i18n — a decision, not an open question

The repo mandates `useTranslation()` for every user-facing string, with translations in `de`/`en`/`it`. Artifact text is currently **server-owned and frozen at emission** (`TableArtifact` renders `columns`/`rows` verbatim; `WeatherArtifact` shows the backend's condition string). Consequences: switching DE→EN leaves prior artifacts in the old language; PWA-cached/persisted artifacts (they live in `message_metadata` and rehydrate on history load) carry the language they were generated in permanently; server-sent counts can't join the client's ICU pluralizer.

**Rule:** SDUI payloads carry **data values only**; every label, button, warning, and unit is rendered **client-side via `useTranslation` keys**. Server-sent *affordance* text is forbidden. This is acceptable for agent-generated read-only content (the model already answered in the turn's language, and the data *is* the content); it is a hard constraint that further shrinks what a `form` payload may carry and another reason interactive/feature UI belongs in slots, where all strings are key-based. The tier-label system (`circle.tier.0..4`) stays i18n-driven, never DB- or server-string-driven.

## 9. Contract versioning (one image, many backends)

The whole model is "one frontend image, many per-instance backends," and `artifactSchema.ts` (frontend zod) and `artifact_service.py` (backend) are **deliberately not codegen-shared** — they drift independently. An older backend emitting a shape a newer frontend tightened (or vice-versa) is the exact skew this doc criticizes federation for. Mitigations:

- Carry a `contract_version` on the artifact envelope; the renderer degrades known-older shapes deliberately rather than by accident.
- The existing fail-closed-to-escaped-code-block path bounds the blast radius, but note the operational failure mode plainly: **after a backend deploy, a not-yet-updated frontend can silently degrade an artifact to a JSON blob** — tolerable for a read-only chart, unacceptable for anything actionable (another reason actionable UI stays in slots, versioned with the shell).
- Slot components (Option B) don't have this problem — they're versioned with the frontend image and talk to the backend over typed REST/WS resources that already evolve together.

## 10. Migration path (incremental, no big bang)

1. **Ship the contribution registry + the `review.section` / `documentRow.*` / `attachment.action` slots**, and move the *existing* flag-gated sections onto them (Simba, dedupe, pdf-split review sections; the doc-row Simba action + status icon). Behaviour-preserving refactor — preserve each section's undo/invalidation/permission state (§5), verify against current behaviour before removing the hand-wiring.
2. **Land the design-compliance gate first** (§7): remediate `ChartArtifact` hex + `DeviceControl` amber + `ArtifactShell` `.card`, add the lint rule over `artifacts/**`. Do this before widening the vocabulary.
3. **New capabilities default to slots for feature UI; read-only agent output defaults to artifacts.** A feature touches the shell only to register a slot contribution.
4. **(Optional, later)** `VITE_FEATURE_*` build flags (Option D) to tree-shake bespoke slot contributions out of an instance's build, if per-instance bundle purity is ever required.

## 11. What the eng/UI/design review changed (2026-08-31)

Three independent reviewers (staff-eng/architecture, UI/interaction, design-system) grounded the first draft against the code and **converged** on one conclusion: the "SDUI-first as the default" headline was wrong and should invert. Specifics folded in:

- **Eng:** "zero frontend code / per-instance-clean for free" was overstated — a new kind is a shell edit shipped to all instances; `form`/`review_card` collapse on the Simba/dedupe stress test; contract versioning was missing; the "~80%" figure had no evidence (dropped). Foundations (reject A, formalize slots, gate-on-flag) verified sound.
- **UI:** enumerated the interactions that don't map to JSON (undo-with-deferred-commit, response-driven reshape, staged confirm, risk-aware focus, dependent selects, cross-surface deep-linking, optimistic reconcile); flagged the "generiert ✨" shell mis-branding a deterministic review queue; showed i18n breaks for feature UI; showed the offline "live controls on a dead socket" cliff. Recommended: registry-first, drop `review_card`, add the §4 checklist.
- **Design:** the existing renderers already drift off-token (hardcoded hex, off-palette amber, no `.card`, zero tier language); SDUI has no design-enforcement seam and the draft referenced `DESIGN.md` only once (in the rejection of A). Recommended the §7 invariants (style-free data, renderer-as-chokepoint, lint gate, tier/severity/provenance schema fields, motion clause) as the guardrail that makes SDUI safe.

Factual corrections also applied: the Simba gate is the `simba_available` local in `services/agent_tools.py` (not a `config._simba_available` attribute); the enforcing CSP is `src/frontend/nginx.conf`; `MergeProposalsSection` self-gates without an `enabled` prop.

## 12. Non-goals / what stays bespoke

- Rich stateful UIs: the Simba two-step irreversible-upload confirm (claim-before-act), chat message branching (`‹ n/m ›` switcher), the 3D Wissensgraph (`GraphView.tsx`), the kiosk constellation, the dedupe/merge review cards. These stay bespoke React, registered via a slot (Option B).
- Micro-frontends / runtime remote bundles (Option A) — explicitly out of scope.
- Interactive artifact kinds beyond the existing `device_control` — out of scope for the vocabulary (§4/§6).
- Cross-instance UI federation — out of scope.

## 13. How this answers the Simba case

- **Doc-row send action + status icon** → `documentRow.action` / `documentRow.statusIcon` slots (reusing the existing tier/status primitives). **Review section** → a `review.section` slot (bespoke React, full fidelity — undo, staged confirm, dependent fields intact). **Two-step irreversible confirm + picker** → bespoke slot component.
- On a **non-Simba instance**, no Simba contribution is registered (the `feature`-flag gate, the UI analogue of #1178) → no Simba UI. With the optional Option-D build flag, no Simba UI *code* either.
- Nothing about Simba maps onto an artifact — it's actionable, irreversible, deep-linked, and stateful, so §4 routes it entirely to slots. That's the honest outcome; the first draft's "~80% expressible as SDUI" claim did not survive review.

## 14. Open questions

- **Contribution registration mechanism** — a static declared manifest (simplest, tree-shakeable via Option D, type-safe) vs a runtime plugin hook. Recommendation: static manifest; Renfield features live in-repo, so a runtime plugin loader adds risk (CSP, versioning) without a real multi-vendor need.
- **Read-only vocabulary line** — which additional *read-only* kinds are worth adding vs left as a slot table. Heuristic: add a kind only when several features want the same read-only shape; otherwise a slot component is cheaper and more flexible.
- **Contract-version policy** — exact envelope field + the degrade matrix across a frontend/backend version skew (§9).

---

**Bottom line:** don't build micro-frontends. Make the **contribution registry the default for feature UI** (formalising the slots that already exist, at full design + interaction + i18n fidelity), and **server-driven typed artifacts the default only for agent-generated, read-only, data-shaped content** — governed by the §4 checklist, the §7 design-compliance invariants, the §8 i18n rule, and the §9 versioning policy. Gate every contribution on the same per-instance flag that gates the backend, and keep build-time tree-shaking as the last resort. This delivers pluggable, per-instance-clean GUI while preserving the integrated single-app UX, the offline/CSP posture, the design system, and the one-shared-image operational model — and it builds on what's already shipped (Lane A artifacts, `widget_tools`, the flag-gated sections) rather than a parallel frontend architecture.
