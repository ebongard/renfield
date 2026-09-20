---
paths:
  - "src/backend/services/meeting_*.py"
  - "src/backend/workers/meeting_worker.py"
  - "src/backend/api/routes/meetings*.py"
  - "voice-server/**/meeting*"
  - "src/frontend/src/pages/Meeting*"
  - "src/frontend/src/components/meetings/**"
---
# Meetings — transcription, minutes, speaker identity

Loaded only when a meeting file is read. Long form: `docs/design/meeting-transcription.md`,
`docs/design/meeting-minutes.md`, `docs/design/meeting-kg-and-speaker-identity.md`.
Flags (all dark by default): `MEETING_TRANSCRIPTION_ENABLED` (+ voice-server `MEETING_ENABLED`; pyannote loads only
then), `MEETING_MINUTES_ENABLED`, `meeting_auto_match_enabled` (deferred). Frontend gates: `meeting_transcription_enabled`
/ `meeting_minutes_enabled` from `/api/config/features` — nav + route absent when off.

## Upload → worker → voice-server
- `POST /api/meetings/transcribe`: **consent REQUIRED, else 422**. Audio is streamed chunk-by-chunk to the shared
  uploads PVC — never whole-file-in-RAM. Returns 202 `{id}`; `retention_until` is stamped from `meeting_retention_days`.
- Per-meeting `language` (`auto` / ISO code) lives on `Meeting.language` (migration `pc20260722c`) and must stay
  threaded worker → `/transcribe-meeting` → whisper. A hardcoded `whisper_language_default=de` returns English
  meetings as hallucinated German.
- `Meeting(status=pending)` (`meetings`, migration `pc20260714`) → `MeetingTaskQueue` (stream `renfield:tasks:meeting`)
  → `workers/meeting_worker.py` (replicas:1): row-level `status` + `heartbeat_at` in-flight guard, poison-pill
  quarantine, and a **4xx-terminal / 5xx-retryable** `VoiceServerError` split (a corrupt recording must fail fast, not
  re-burn the GPU).
- Voice-server `voice-server/voice_server/services/meeting_service.py`: `align_words_to_segments` stays PURE and
  fixture-tested. **ECAPA, not whisper, is the GPU-OOM risk** — `speaker_service.cap_clip` bounds the ECAPA input
  to a centered window (`speaker_embed_max_seconds`, 30 s); `meeting_chunk_seconds` is only a backstop.
- Frontend upload must set `timeout: 0` — the shared `apiClient` aborts at its 30 s default.

## Transcript document
- Ingest via `folder_ingest.ingest_document` with `source="meeting_transcript"` (`documents.source`, migration
  `pc20260714b`): this **gates Schicht-A OFF** (D14 — no phantom obligations from small talk) and sets
  `file_to_paperless=False`.
- Attribution = honest pseudonyms ("Sprecher N") + human relabel (`POST /api/meetings/{id}/relabel`): re-render →
  **reindex in place**, stable `transcript_document_id`. Never a second ingest.
- The per-cluster ECAPA `embedding` is **NEVER** stored on `Meeting.segments`, regardless of flags —
  `meeting_pipeline.strip_biometric_fields` / `_set_segments` is the only segments write path.
- `kg_post_document_ingest_hook` runs `_strip_speaker_pseudonyms` (gated `source == MEETING_TRANSCRIPT_SOURCE`, narrow
  `Sprecher N:` / `Speaker N:` line-prefix regex) BEFORE KG extraction, so a pseudonym never becomes a person
  entity that collides across meetings; a relabelled real name is preserved.
- Delete (owner-gated) + the retention sweep share ONE cascade: `meeting_retention.purge_meeting` (transcript doc +
  audio + row). `GET /api/meetings` is owner-scoped, newest-first, capped 1-200.

## Minutes (`services/meeting_minutes.py`)
- Lifecycle `minutes_status`: `none` → `draft` → `confirmed` (migration `pc20260718_meeting_minutes`). Routes are
  owner-gated 404 and flag-gated 404 via `_require_minutes_enabled`. Generate: 409 unless `completed`; PUT always
  reverts to `draft`; confirm: 409 unless `draft`.
- Confirm renders the minutes into the **SAME transcript document** (`render_transcript_markdown` +
  `_overwrite_transcript_and_reindex`) — never a second ingest.
- Action items are meeting-scoped, **deliberately NOT obligations** (`due_hint` is a verbatim string, not a computed date).
- Extractor failure → `empty_minutes()`; `_normalize_minutes` enforces caps. Typed JSON in/out, no model HTML.
- Frontend: **confirm auto-saves a dirty body first** (PUT → draft → confirm) so unsaved edits are never dropped.
