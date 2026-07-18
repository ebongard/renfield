# §2 Phase 3 — Meeting Minutes Pipeline (PLAN)

> Status: **PLAN / not started.** Builds on Phase 2 (meeting transcription + diarization,
> live on household + xidra). Turns a completed, speaker-attributed transcript into
> structured **minutes** — summary + decisions + action-items — with a **human-confirm**
> gate before anything is finalized or ingested. Flag-gated, dark by default.

## Goal & scope

From a `completed` `Meeting` (segments + `transcript_document_id` already exist), one LLM
pass produces a draft **minutes** object:

- **summary** — a few sentences.
- **decisions[]** — each `{ text, made_by? }` (speaker attribution optional).
- **action_items[]** — each `{ text, owner?, due_hint? }` — meeting-scoped, human-confirmed.

The draft is **proposed, never auto-committed**. The owner reviews/edits, then confirms;
the confirmed minutes render into the transcript document (or a sibling doc) in the KB.

### Explicit non-goals (locked in the §2 design)
- **Action-items are NOT obligations.** §2 already gates Schicht-A **off** on transcripts
  (D14 — "no phantom obligations from small talk"). Minutes action-items stay meeting-scoped
  and human-confirmed; they do **not** feed the obligation notifier / calendar sync. (A future
  "promote this action-item to an obligation" affordance could be added deliberately, later.)
- **Satellite recording** ("Renfield, starte Meeting-Aufnahme") is a separate later phase,
  out of the numbered Phases 1–4.
- **Auto-confirm** — out. Human-in-the-loop is the whole point of this phase.

## Data model (additive migration)

The `meetings` table has no minutes fields today. Add them additively (migration
`pc2026XXXX_meeting_minutes`, chain off the current head):

```
minutes            JSONB  NULL   -- {summary, decisions[], action_items[]} (draft or confirmed)
minutes_status     VARCHAR(20) NOT NULL DEFAULT 'none'
                    -- none → draft → confirmed  (mirrors the meeting status machine)
minutes_generated_at  DateTime NULL
minutes_confirmed_at  DateTime NULL
```

- One JSONB column keeps it cross-dialect (same pattern as `segments`), avoids N child tables
  for v1, and rehydrates trivially. If decisions/action-items later need per-row state
  (e.g. individual action-item completion), promote to a `meeting_minutes_items` table then —
  not now (YAGNI).
- `minutes_status` is the gate: `none` (not generated) → `draft` (LLM produced, awaiting
  confirm) → `confirmed`. The UI and ingest key off this.

## Extraction service (`services/meeting_minutes.py`)

Mirror the Schicht-A extractor pattern (`services/schicht_a_extractor.py`):

- `class MinutesExtractor` with `async def extract(segments, *, lang="de") -> MinutesDraft`.
- Build the prompt from the **attributed** segments (speaker labels + text), so decisions/
  action-items can carry `made_by`/`owner` when the transcript names a person.
- Use the **thinking-model-aware** chat kwargs helper (`get_classification_chat_kwargs`) —
  the same one Schicht-A uses — so a qwen3-style thinking model doesn't return empty `content`
  (known failure mode: `reference_person_names_embedding_cluster` sibling — thinking models +
  `content` handling).
- **Structured output**: request strict JSON (`{summary, decisions, action_items}`); validate
  the shape server-side before storing (kind/size caps like `artifact_service`), fall back to
  a warm empty draft on a malformed response rather than persisting garbage.
- New prompt file `prompts/meeting_minutes.yaml` (de + en variants), version-hashed like the
  other prompts (shows up in `/health` prompt_hashes).

## Pipeline: when it runs

**Decision D-M1 — on-demand, not auto.** After a meeting completes, `minutes_status='none'`.
The user clicks **"Protokoll erstellen"** on a completed meeting → `POST
/api/meetings/{id}/minutes/generate` enqueues (or runs inline for short meetings) the extractor
→ stores `minutes` + `minutes_status='draft'`. Rationale: avoids an LLM pass on every meeting
(cost), and many recordings won't need minutes. (Auto-draft-on-complete is a trivial later
flip if wanted — a follow-on step in `meeting_worker` after ingest.)

For long transcripts the generate call routes through the **meeting worker** (reuse the queue
+ heartbeat guard) rather than blocking the request; short ones can run inline. Start inline
with a size threshold; add the queue path only if needed.

## API surface (routes on `api/routes/meetings.py`, owner-gated like the rest)

```
POST   /api/meetings/{id}/minutes/generate   -> 202/200, sets minutes_status=draft (409 if not completed)
GET    /api/meetings/{id}/minutes            -> {minutes, minutes_status, ...}
PUT    /api/meetings/{id}/minutes            -> owner edits the draft (summary/decisions/action_items)
POST   /api/meetings/{id}/minutes/confirm    -> minutes_status=confirmed + render into KB (409 unless draft)
DELETE /api/meetings/{id}/minutes            -> back to none (discard draft)
```

All `_require_enabled()` + `_get_owned_meeting()` gated, same as the existing meeting routes.

## Human-confirm + ingest

- Minutes are their **own** confirm surface (a REST confirm on the meeting), **not** the chat
  `paperless_confirm` WS-frame path — meetings live on their own page/API, not in the chat flow.
- On **confirm**: render the confirmed minutes as markdown and **append/prepend to the transcript
  document** via the same stable-doc reindex path re-attribution uses
  (`meeting_pipeline._reindex_transcript`, stable `transcript_document_id`) — OR ingest a sibling
  "Protokoll" doc. **Decision D-M2:** append to the transcript doc (one KB artifact per meeting,
  simpler retention — the existing retention job already purges the transcript doc). Keep
  Schicht-A **off** on it (same `source="meeting_transcript"` gate).

## Frontend (`pages/MeetingsPage.tsx`)

Extend the expanded completed-meeting view (below the transcript / relabel section):

- If `minutes_status === 'none'`: a **"Protokoll erstellen"** button → generate.
- If `draft`: render summary + decisions + action-items as **editable** fields (typed inputs,
  add/remove rows) + **"Bestätigen"** / **"Verwerfen"**. Reuse the inline-confirm affordance
  style already on the card (relabel/delete).
- If `confirmed`: read-only minutes + a link to the KB doc (already have `transcript_document_id`).
- New `api/resources/meetings.ts` hooks (`useMeetingMinutes`, `useGenerateMinutes`,
  `useUpdateMinutes`, `useConfirmMinutes`) — same `useApiQuery`/`useApiMutation` idiom.
- i18n `meetings.minutes.*` in **de + en**. Typed shapes (no `any`).

## Config / flag

`MEETING_MINUTES_ENABLED` (default **false**, dark). Gate the routes (404 when off) + the
frontend affordance (surface via `/api/config/features`, like `meeting_transcription_enabled`).
Needs a chat model (already available); no new infra.

## Build sequence (PR breakdown)

1. **PR-A (backend):** migration (additive minutes fields) + `MinutesExtractor` +
   `prompts/meeting_minutes.yaml` + the 5 routes + flag. Tests: extractor (fixture transcript →
   shaped minutes, malformed-LLM fallback), route owner-gating + status transitions
   (none→draft→confirmed, 409s), confirm→reindex. `test_meetings.py`.
2. **PR-B (frontend):** minutes section on MeetingsPage + hooks + i18n + RTL tests
   (generate/edit/confirm/discard, flag-gated render). Expose the flag in `/api/config/features`.
3. **Docs sweep + deploy** (backend then frontend, **pre-seed images over LAN** — the Recreate
   WAN-pull outage bit us 3× in the Phase 2 rollout).
4. **Flag flip** per instance + browser E2E (generate → edit → confirm → KB doc updated).

## Open decisions to confirm before building

- **D-M1** on-demand vs auto-draft-on-complete (plan: on-demand). 
- **D-M2** append-to-transcript-doc vs sibling Protokoll doc (plan: append).
- **D-M3** action-item → obligation promotion: **out of v1** (keep the D14 wall); revisit as its
  own opt-in later.
- **Language**: extract in the transcript's language (de default), like the transcript.

## Ties into
Phase 2 design `docs/design/meeting-transcription.md` (the transcript this consumes);
`services/schicht_a_extractor.py` (extraction pattern); the additive-migration discipline in
`CLAUDE.md` (Alembic transaction model). Phase 4 (Notes + project timeline) builds on top:
confirmed minutes are a natural timeline event + a note source.
