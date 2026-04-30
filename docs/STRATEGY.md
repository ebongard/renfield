# Strategy — Renfield

> **Status:** Draft skeleton (2026-04-30). Sections marked **[FOUNDER FILL-IN]** can only be answered by Eduard. Everything else is factual and verifiable from code or sibling docs. Update this doc when the strategic bet shifts; archive it (don't delete) when it's superseded.

---

## Why this doc exists

This is the founder's letter for Renfield. It captures the strategic intent — distinct from the architecture (`docs/SECOND_BRAIN.md`), the visual system (`DESIGN.md`), and the feature backlog (`docs/private/feature-ideen.md`). Those documents answer **WHAT** and **HOW**. This one answers **WHY**, in a form that survives session compaction, agent memory rot, an 18-month sabbatical, or a project handoff.

It is deliberately written so a contributor opening the codebase 18 months from now (with no Slack history, no recent conversation, no founder available) can understand which axis to evaluate future work on.

**This is not a fundraising deck.** Renfield is a self-hosted, open-source, single-founder project. There is no board to justify the bet to. The strategic premise, ultimately, is *"I want this thing to exist in the world"* — and that is a legitimate and sufficient answer for a project of this shape. The rest of the doc is the **scaffolding** around that conviction: what specifically the founder wants to exist, what alternatives were considered, what the failure modes look like, and what would invalidate the bet.

---

## What Renfield is (one paragraph)

Renfield is a fully offline-capable, self-hosted digital assistant for households. Voice satellites in every room (Pi Zero 2 W + ReSpeaker), local LLMs via Ollama, multi-user from day one, with a "second brain" knowledge layer that unifies four information types (KB documents, knowledge-graph entities/relations, conversation memory, and federated peer answers) under a single Atoms abstraction. The product lives in the kitchen, not at a desk. Adjacent industry: Home Assistant, Mem.ai, Reflect, Tana — but none of those are voice-first, multi-user, household-scoped, *and* offline. See `docs/SECOND_BRAIN.md` for the architecture, `DESIGN.md` for the visual differentiation thesis.

---

## The strategic bet

Renfield is built on three nested bets, in dependence order:

### Bet 1 — Voice + multi-user + offline + household is an underserved combination
Every PKM/second-brain product converges on clinical desk-bound single-user. Every voice assistant converges on cloud-bound surveillance-economic single-account. Renfield refuses both. The conviction is that there is a real product space at the intersection that no incumbent occupies and no incumbent is structurally able to occupy (cloud-economic players can't go offline; productivity players can't go household; voice-assistant incumbents can't go multi-user without breaking their identity model).

**[FOUNDER FILL-IN: in 2-3 sentences, what specifically convinces you that this gap is real and durable rather than a niche-trap?]**

### Bet 2 — Reva validates the framework thesis
Reva (`/Users/evdb/projects.ai/reva`) is an Enterprise Teams chatbot built on top of Renfield as a git submodule. As of 2026-04-26 (PR #177), all 8 features Reva needed from Renfield are landed in `main` and verified on PRD. Reva works. That's a real signal — *the same framework actually does serve both Home and Enterprise* — not a thesis. See `~/.claude/projects/-Users-evdb-projects-ai-renfield/memory/project_reva_compatibility.md` for verification details.

The strategic move from this validation: keep Reva and Renfield genuinely unified in `main` (don't fork) so future feature work in either repo amortizes across both. Cost: Renfield carries a few abstractions (`AgentRole.utterances`, `EpisodicMemory`, `ConversationMemory.team_id`) that Home doesn't strictly need. Worth it because forking would compound forever.

### Bet 3 — Federation matters even before there's a second peer
Circles v1 ships peer-to-peer federation infrastructure: pairing handshake, signed query/response, audit trail, tier-reach across instances. As of this writing, **no second Renfield instance has paired with the user's**. The infrastructure exists for a population of 1.

This is the most honest place in the doc. The bet here is that:
- The cost of building federation *into* the data model from day one is much lower than retrofitting it later (the polymorphic `atom_id` + `circle_tier` columns on every retrieval table, the `circle_resolver` policy evaluator, the per-row tier filter in `circle_sql.py` — all of these would be much harder to add to a system that already has data).
- The product use-case (asking your sister's Renfield for a recipe; asking a friend's Renfield for a contact recommendation) is a ten-year horizon, not a six-month one.
- A self-hosted, federated household assistant is the *only* shape this product can take — a centrally-hosted version contradicts the offline + privacy positioning at the root.

**Field-of-dreams risk:** if no second peer ever paired in 24 months, the federation infrastructure becomes dead weight. The bet only pays out if Renfield reaches enough users that two of them happen to know each other. That's a real risk.

**[FOUNDER FILL-IN: what's the realistic pairing horizon you'd accept before declaring the federation bet failed and ripping it out? 12 months? 36? Never (it's intrinsic to the product even at population 1)?]**

---

## What "winning" looks like

In rough order of how you'd know the bets are paying out:

1. **Reva continues to require less and less Renfield-specific patching.** The framework abstraction holds. (Already substantially true.)
2. **A second household adopts Renfield voluntarily.** Not as a favor, not as a contractor relationship — someone deploys it because they read the docs and wanted it. (Not yet true.)
3. **Two paired Renfield instances actually exchange a federated query in production.** (Not yet true.)
4. **A third-party builds an MCP integration for Renfield specifically** (not a generic integration). Indicates the platform thesis registers externally.
5. **The "household" framing shows up unprompted in someone else's product writing**, even as a competitor. Means the positioning landed.

---

## What was considered and not chosen

The maximalist circles plan was picked over four alternatives, listed here so the path-not-taken is inspectable:

| Alternative | What it would have looked like | Why not chosen |
|---|---|---|
| **6-week MVP** | Single-user, no federation, no circles, ship fast, see if people want it | **[FOUNDER FILL-IN: why this didn't fit. Probably: "the MVP would have validated 'a chatbot' which is already validated; the bet I'm actually making isn't testable in 6 weeks"]** |
| **Public Renfield launch (cloud-hosted)** | Free tier on x-idra.de servers, drop the offline requirement | **[FOUNDER FILL-IN: probably: contradicts the data-ownership positioning that's the core differentiator]** |
| **Reva commercial-first** | Push Reva as a paid Enterprise product, treat Renfield as the framework byproduct | **[FOUNDER FILL-IN: probably: enterprise sales cycle is 12-18 months and would consume bandwidth that's currently going to the household-product thesis]** |
| **Small household features** | Stop after circles v1, build the long tail in `feature-ideen.md` (shopping lists, n8n routines, dashboard widgets) | **[FOUNDER FILL-IN: probably: those features are derivative of the platform being there; without circles + federation the platform thesis stays unvalidated]** |

The honest version of all four explanations: **the founder wants to build the maximalist version because that's the version that's interesting to him personally, and since he's funding it himself, that's a sufficient reason.** The table above is the version a future contributor or reviewer would expect; the parenthetical is the truth.

---

## What would invalidate the bet

The strategic premise should be re-examined if any of the following happen:

- **24 months without a second adopter.** Federation infrastructure becomes dead weight. Either the positioning is wrong or the distribution problem is unsolved.
- **A cloud-hosted incumbent (Apple, Google, Mycroft successor) ships voice + multi-user + household with a believable privacy story.** Renfield's offline differentiation collapses. Hard to imagine, but not impossible.
- **Reva diverges enough that the framework abstraction breaks.** If Renfield ends up patched with `if reva: ...` branches in core paths, the unification thesis was wrong and the right move is to fork.
- **The founder concludes that the federation bet is wishful thinking** and the household-only product is sufficient. In that case: rip out the federation paths, simplify the schema (drop `atom_explicit_grants`, `circle_memberships`, `circle_resolver`'s peer paths), keep circles v1 as a per-user privacy mechanism only.

**[FOUNDER FILL-IN: are there other signals you'd treat as invalidating? E.g. a particular adoption number, a competitor move, a personal-energy threshold.]**

---

## Five-year ideal state

**[FOUNDER FILL-IN: 1-2 paragraphs. What does "Renfield won" look like in 2031? How many households? Federation density? Reva relationship? Your personal involvement (still maintaining? handed off? sold)?]**

This section is the most uncomfortable to write because it forces commitment to a vision that may not pan out. Write it anyway — vagueness here is what the outside-voice critique was warning about. A concrete, falsifiable five-year picture is more valuable than a hedged inspirational one, even (especially) if the picture turns out wrong.

---

## Audience and lifecycle

This doc has three readers, in priority order:

1. **The founder, 18 months from now**, returning after a sabbatical or Reva commercial focus. Needs the conviction externalized so it can be picked back up without re-deriving it.
2. **A future maintainer or contributor**, evaluating whether to commit time. Needs to know whether the project's strategic frame still holds before investing.
3. **An eng/CEO review of v2.5 / v3 work**, evaluating whether a proposed change still serves the original intent. Needs the bet stated clearly enough to compare against.

This doc should be **revised, not appended to.** When the bet shifts, rewrite the affected sections; archive a snapshot under `docs/strategy-archive/YYYY-MM-DD.md` if the change is large. Stale strategy is worse than no strategy.

---

## See also

- `docs/SECOND_BRAIN.md` — atoms, retrieval, federation architecture
- `DESIGN.md` — visual + interaction system, household-product aesthetic positioning
- `docs/CIRCLES.md` — circles v1 user-facing + architectural docs
- `docs/private/feature-ideen.md` — feature backlog, paths not taken
- `~/.claude/projects/-Users-evdb-projects-ai-renfield/memory/project_reva_compatibility.md` — Reva integration verified-on-PRD record
- `tasks/audit-findings-plan.md` — completed EMPFEHLUNG audit (E1–E18 closed 2026-04-30)
