---
name: add-skill
description: Guide for adding a new procedural SKILL to Renfield — a reusable recipe (triggers + tool sequence + step-by-step body) that's injected into the agent prompt on a matching turn. Triggers on "add skill", "neuer Skill", "Skill hinzufuegen", "seed skill", "procedural skill", "teach Renfield a workflow", "Renfield eine Prozedur beibringen".
---

# Adding a Renfield Skill

A **procedural skill** teaches the agent *how* to do a task — which tools to call, in
what order, with which guardrails. On a matching turn its body is injected into the
agent prompt. You author **seed skills** (Markdown files in git); the self-learning
system auto-extracts the rest.

Full reference: **`docs/SKILLS.md`**. This is the quick build workflow.

> Requires `SKILLS_ENABLED=true` on the target instance (default off; on for xidra).

## Quick Start (4 steps)

### 1. Create `src/backend/seed_skills/<verb_object>.md`

```markdown
---
title: <unique, human title>          # dedupe key — must be unique
triggers:                             # user phrasings that make it FIRE (de + en)
  - "buche meine Rechnungen beim Steuerberater"
  - "sync my invoices to the accountant"
tools:                                # tools the recipe uses (advisory metadata)
  - mcp.accountant.validate
  - mcp.accountant.submit_invoice
---
- Schritt 1: Hole die noch nicht synchronisierten Rechnungen mit <tool>.
- Schritt 2: Prüfe sie mit `mcp.accountant.validate(...)` (Vorschau, NICHTS buchen).
- Schritt 3: Erst NACH Bestätigung mit `mcp.accountant.submit_invoice(...)` buchen.
- Berichte NUR das Tool-Ergebnis; erfinde keine Buchungs-IDs.
```

Model it on an existing file — `src/backend/seed_skills/send_paperless_document_by_email.md`
is a good multi-step, documents-domain example.

### 2. Get the triggers right — this is what makes or breaks it

Matching is **semantic** (pgvector cosine over `title + triggers + first 200 chars of
body`, threshold 0.75, top-3). Write **3–6 realistic paraphrases per language** you
support. Thin triggers = a skill that never fires.

### 3. Make sure the tools actually exist in the routed role (the #1 gotcha)

The `tools:` list is advisory — the agent can only **call** tools its **role** exposes.
- Confirm the tools exist: an MCP server in `config/mcp_servers.yaml` (add one with
  `/add-integration`) or a registered `internal.*` tool.
- Confirm the role that these queries route to (`config/agent_roles.yaml` +
  `prompts/router.yaml`) **includes** those tools in its `mcp_servers` / `internal_tools`.
- If not, add them to the role. Otherwise the agent reads the recipe but has no tool to
  run and may hallucinate a wrong action.

### 4. Ship + verify

- The file loads at **boot** (idempotent by title — no migration). Restart the backend
  on an instance where `SKILLS_ENABLED` is on.
- Boot log: `🌱 … seed skill(s) loaded`; on a matching turn: `🧠 N procedural skill(s)
  injected`.
- **Verify through Renfield chat** — ask it using a trigger phrasing and confirm it
  follows the recipe. Let *Renfield* run the action; never invoke the underlying data
  tools by hand (no `kubectl exec`, no direct DB — that does Renfield's job for it and
  leaks prod data).

## Checklist

- [ ] `title` is unique
- [ ] triggers cover de + en, 3–6 realistic paraphrases
- [ ] every tool in `tools:`/body exists AND is in the routed role
- [ ] body is imperative, concise, with the key `IMMER`/`NIEMALS` guardrails
- [ ] a dry-run/confirm gate for any destructive or outward-facing action
- [ ] verified via a real chat turn, not by calling tools directly

## See Also
- `docs/SKILLS.md` — full reference (matching/injection mechanics, config, file map)
- `/add-integration` — add the MCP server a skill's tools come from
- `/add-hook` — context injection / post-processing / registering agent tools
