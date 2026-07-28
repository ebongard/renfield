# Extending Renfield with Skills

A **procedural skill** is a short, reusable recipe that tells the agent *how* to
accomplish a task — which tools to call, in what order, and what to watch out for.
When a user's turn matches a skill, its body is injected into the agent prompt so
the ReAct loop follows a consistent, proven procedure instead of re-deriving it.

Skills are the third Renfield extension point, alongside MCP integrations
(`/add-integration`) and hooks (`/add-hook`). Use the **`/add-skill`** dev workflow
to scaffold one.

> Gated by `SKILLS_ENABLED` (default off; on for xidra). With the flag off, nothing
> below runs.

---

## Two kinds of skill

| | Seed skill | Auto-learned skill |
|---|---|---|
| **Origin** | Hand-written, shipped in git | Extracted from real successful turns |
| **Lives in** | `src/backend/seed_skills/*.md` | `procedural_skills` DB table |
| **Owner / tier** | System-owned, public tier (visible to all) | The user whose turn produced it |
| **Loaded by** | `skill_seed_loader.load_all_seeds` at boot | `trajectory_capture` → `skill_curator` |
| **You author** | ✅ this doc | (automatic; curated/deduped by `skill_curator_service`) |

This doc is about **seed skills** — the ones you write by hand. Auto-learned skills
use the same DB shape and the same matching/injection path; the self-learning system
(`docs/design/self-learning-admin-console.md`) manages them.

---

## The seed-skill file format

A seed skill is a single Markdown file in `src/backend/seed_skills/`: YAML-ish
front-matter delimited by `---`, then a Markdown body.

```markdown
---
title: Album auf DLNA-Renderer abspielen
triggers:
  - "spiel das Album X im Wohnzimmer"
  - "Album X im Wohnzimmer"
  - "play album Y in the kitchen"
tools:
  - mcp.media.search_media
  - internal.play_album_on_dlna
---
- Schritt 1: Suche das Album mit `mcp.media.search_media(query="<name>", type="MusicAlbum")`.
- Schritt 2: Spiele es mit `internal.play_album_on_dlna(album_id="<id>", renderer_name="<Raum>")`.
- NIEMALS `mcp.dlna.play_tracks` direkt aufrufen.
- Nach Erfolg SOFORT `final_answer` mit einer kurzen Bestätigung.
```

### Front-matter fields

| Key | Required | Type | Notes |
|---|---|---|---|
| `title` | ✅ | string | **Unique** — the loader dedupes by title (`source="seed"`), so re-running at every boot is safe. Renaming a title creates a *new* skill; the old one lingers until removed. |
| `triggers` | ✅ | list | User-phrasing variants. These drive the **semantic match** (see below), so include realistic paraphrases in every language you support (de + en). |
| `tools` | ⬜ | list | The tools the recipe uses (`mcp.<server>.<tool>` / `internal.<tool>`). Advisory metadata — see the tool contract below. |

The parser is a small hand-rolled reader (not full YAML — no PyYAML dependency). Keep
the front-matter to these three keys, `- ` / `  - ` list items, one `key: value` per
line. Quotes are stripped. A file missing `title`, `triggers`, or a body is skipped
with a warning at boot.

### The body

Plain Markdown. Write it as **imperative instructions to the agent**, the same voice
as the role prompts in `prompts/agent.yaml` — short bullet steps, the exact tool calls
with argument names, and the `NIEMALS …` / `IMMER …` guardrails that matter. This text
lands verbatim in the agent's prompt on a match, so be precise and concise.

---

## How a skill reaches the agent

1. **Boot load** — `skill_seed_loader.load_all_seeds` reads every `*.md` in
   `settings.skill_seed_directory` (default `seed_skills`, relative to `src/backend`),
   gated by `SKILL_SEED_LOAD_ON_BOOT` **and** `SKILLS_ENABLED`. Each file becomes a
   `procedural_skills` row; existing titles are skipped (idempotent).
2. **Match** — on each turn, `SkillService.find_similar(message, asker_id)` runs a
   **pgvector cosine** search. The match embedding is `title + triggers + first 200
   chars of body` — so the **triggers are what make a skill fire**. Gated by
   `SKILL_INJECT_SIMILARITY_THRESHOLD` (default 0.75) and `SKILL_INJECT_TOP_K`
   (default 3).
3. **Inject** — matched skills are rendered by `SkillService.format_for_prompt` into
   the `{learned_skills}` block of the agent prompt (`agent_service.py:~1210`), gated
   by `SKILLS_ENABLED` **and** `SKILL_INJECT_ENABLED`.
4. **Feedback** — injected skill IDs are recorded on the turn; the post-turn task bumps
   each skill's success/failure count (the self-learning loop).

---

## The tool contract (the #1 gotcha)

A skill's `tools:` list and the tool names in its body are **advisory** — they tell the
LLM which tools to reach for. But the agent can only actually **call** tools that its
**routed role** exposes. Roles are defined in `config/agent_roles.yaml`
(`mcp_servers` + `internal_tools`).

So a skill only works end-to-end if:
- the user's turn **routes to a role** (`agent_router` / `prompts/router.yaml`) that
- **has the tools** the skill uses (in that role's `mcp_servers` / `internal_tools`).

If your skill uses `mcp.accountant.submit_invoice`, that MCP server must be wired into
the role the query lands in (e.g. `documents`), or the agent will read the recipe but
have no tool to run — and may hallucinate a wrong action. Verify the routing + role
tools, not just the skill file.

---

## Add a seed skill — step by step

1. **Write** `src/backend/seed_skills/<verb_object>.md` in the format above. Model it
   on an existing one (`send_paperless_document_by_email.md` is a good multi-step,
   documents-domain example).
2. **Triggers**: 3–6 realistic paraphrases per language you support. These are the
   whole ballgame for whether it fires — under-specified triggers = a skill that never
   matches.
3. **Tools**: list only tools that a real role has. If the skill needs a new tool,
   first add it (`/add-integration` for an MCP server, or register an `internal.*` tool)
   and wire it into the role in `config/agent_roles.yaml`.
4. **Verify the routing**: confirm the queries in your triggers route to a role that
   owns those tools (check `config/agent_roles.yaml` + `prompts/router.yaml`).
5. **Ship**: the file loads at boot (idempotent). No migration. On an instance where
   `SKILLS_ENABLED` is on, restart the backend to pick it up.

### Verify (no prod data needed)
- Boot logs: `🌱 … seed skill(s) loaded` and, on a matching turn, `🧠 N procedural
  skill(s) injected into agent prompt`.
- The skill row: `GET /api/skills` (or the admin console) shows it under `source=seed`.
- Functionally: ask Renfield in chat using one of the trigger phrasings and confirm it
  follows the recipe. (Let Renfield run the action — don't invoke the underlying tools
  by hand.)

---

## Config reference

| Setting | Default | What it does |
|---|---|---|
| `SKILLS_ENABLED` | `false` | Master switch for the whole feature |
| `SKILL_SEED_LOAD_ON_BOOT` | `true` | Load `seed_skills/*.md` at boot |
| `SKILL_SEED_DIRECTORY` | `seed_skills` | Seed dir, relative to `src/backend` |
| `SKILL_INJECT_ENABLED` | `true` | Inject matched skills into the agent prompt |
| `SKILL_INJECT_TOP_K` | `3` | Max skills injected per turn |
| `SKILL_INJECT_SIMILARITY_THRESHOLD` | `0.75` | Min cosine similarity to inject |
| `SKILL_CURATOR_ENABLED` | `false` | Background dedup/merge of auto-learned skills |

---

## Where things live (quick map)

| Piece | Path |
|---|---|
| Seed skill files | `src/backend/seed_skills/*.md` |
| Boot loader | `src/backend/services/skill_seed_loader.py` |
| CRUD + similarity retrieval | `src/backend/services/skill_service.py` |
| Curator (auto-learned dedup/merge) | `src/backend/services/skill_curator_service.py` |
| DB model | `procedural_skills` (`src/backend/models/database.py`) |
| REST API | `src/backend/api/routes/skills.py` |
| Prompt-injection point | `src/backend/services/agent_service.py` (`find_similar` → `{learned_skills}`) |
| Role → tools mapping | `config/agent_roles.yaml` |
| Self-learning system design | `docs/design/self-learning-admin-console.md` |

## See also
- `/add-skill` — guided workflow to scaffold a seed skill
- `/add-integration` — add the MCP server a skill's tools come from
- `/add-hook` — the other extension point (context injection, post-processing, tools)
