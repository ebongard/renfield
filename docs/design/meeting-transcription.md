# Meeting Transcription & Diarization — Design (§2 of the meeting/project workflow)

> Status: **SHIPPED — LIVE on both instances** (`MEETING_TRANSCRIPTION_ENABLED=true` in
> `k8s/configmap.yaml`; as-built summary at the end of this doc, invariants in
> `.claude/rules/meetings.md`). Reviewed 2026-07-06 (/plan-eng-review, 9 review findings + 12
> outside-voice findings resolved). Generic feature for BOTH instances (household +
> work). Business-instance phasing lives in the private instance plan.

## Goal

Upload a multi-speaker meeting recording → speaker-attributed transcript → retrievable
in the knowledge base (RAG + Wissen workspace). Written notes and single-voice dictation
already work via existing paths; this adds the multi-speaker piece.

> Related design: `docs/design/voice-identity-wakeword-verification.md` plans ONLINE
> (streaming) diarization for session continuity — a different pyannote mode than the
> batch pipeline here. Coordinate the voice-server image change (GPU torch + pyannote
> layer split) so both land ONE pyannote integration, not two.

## Locked architecture (all decisions from the review)

```
 Client (phone/dictaphone recording — upload-first, D15)
   │  POST /api/meetings/transcribe  (multipart; consent_confirmed REQUIRED → else 422)
   │  nginx: client_max_body_size raised per-location on /api/meetings/ only (D3)
   ▼
 Backend route (stream-to-disk → persistent documents storage, never whole-file-in-RAM)
   │  creates Meeting(status=pending) ── 202 {meeting_id}
   │  XADD renfield:tasks:meeting  (payload = audio path, NOT bytes)
   ▼
 Meeting worker (own Redis-Streams consumer group — DocumentTaskQueue pattern, D2)
   │  ├─ max-duration cap (default 4h, configurable) enforced at upload → visibility
   │  │  window derived from cap; heartbeat timestamp on Meeting row while running
   │  ├─ in-flight guard: redelivery sees status=processing + fresh heartbeat → wait,
   │  │  status=completed → ack+skip (idempotent pipeline)   (D13)
   │  ▼
   │  voice-server POST /transcribe-meeting   (pod-to-pod, long httpx timeout)
   │    ├─ pyannote.audio diarization (model BAKED into image at build — HF-gated,
   │    │  offline-first; GPU torch added via existing layer-split pattern)  (D5)
   │    ├─ faster-whisper, meeting_whisper_model knob, lazy-load/unload per job (D10)
   │    ├─ alignment: word_timestamps=True + interval overlap w/ overlap rules —
   │    │  pure logic, fixture-unit-tested, NO GPU needed in tests
   │    ├─ per-cluster ECAPA embedding in the voice-server ONNX space (D4)
   │    └─ batch semaphore=1; live-STT p95 measured during batch in the spike;
   │       escalation ladder: night-window scheduling (daypart) → process priority →
   │       separate deployment. Threshold: live p95 ≤ 2× baseline.
   │  ▼
   │  attribution (backend): DEFAULT = honest pseudonyms ("Sprecher N") + one-click
   │  human labeling. Auto-match is SPIKE-GATED (D12): built only if cluster
   │  separation on meeting audio ≥ 0.15 (same-speaker − diff-p95); if built, a NEW
   │  read-only matcher (pure function, margin-gated like speaker_resolver but NO
   │  commits / NO review-bucket writes / NO reinforcement) — NOT built, not
   │  planned (2026-09-20; the placeholder flag was removed).
   │  ▼
   │  segments JSONB on Meeting row (D7) + rendered markdown →
   │  folder_ingest.ingest_document() into the target KB (D6): dedup, chunking,
   │  embeddings, circle tier — but Schicht-A extraction GATED OFF for
   │  source=meeting_transcript (D14: no phantom obligations/calendar events from
   │  small talk; purpose-built action-item extraction comes with the minutes phase).
   │  file_to_paperless=False for transcripts.
   ▼
 GET /api/meetings/{id} — status poll (pending/processing/completed/failed), reach-gated (auth-on §8.1)
 GET /api/meetings     — reach-scoped list, newest-first, capped 1-200 (backs the frontend list; added PR-3)
 DELETE /api/meetings/{id} — OWNER-gated whole-meeting delete (transcript doc + audio + row; shared purge_meeting)
```

**Re-attribution** (pseudonym → person, or fixing a name): update segments → re-render →
**existing reindex path** (update document content + purge/rebuild chunks) — never a new
`ingest_document` call (content-hash dedup would mint a second document and orphan the
first). `transcript_document_id` stays stable.

**Audio lifecycle:** original audio deleted after `completed` + grace period (default);
`meeting_keep_audio=true` opt-in. **Retention is a mechanism, not a column:** a daily
retention job deletes expired meetings (per `retention_until`) via the existing
document-delete path (purges chunks/facts) + segments + audio.

## Meeting table (lean §2 migration)

`id, owner_user_id, circle_tier, title, date, status, error, heartbeat_at,
segments JSONB, transcript_document_id, consent_confirmed, consent_note,
retention_until, created_at`

- NO `project_id` in §2 (Project table doesn't exist yet — added additively with the
  project-model migration later). NO minutes fields (summary/decisions/action_items —
  additive migration with the minutes phase).
- `consent_confirmed` is REQUIRED at upload from day one (DE workplace recording;
  "designed in, not bolted on").

## Spike (blocks the build) — `bin/run_diarization_eval.py`

Built as a PERSISTENT eval harness (D9), not a throwaway. Two-tier fixtures:
committed synthetic/public reference (privacy-clean regression anchor) + a gitignored
local directory of real room recordings (actual acoustics; voices never committed).

Measures, with HARD gates fixed BEFORE running:
1. Diarization/attribution error rate on the reference (abort criterion — define the
   acceptable ceiling before measuring).
2. GPU-s per audio-minute + VRAM concurrency for base vs medium vs large-v3 →
   sets the `meeting_whisper_model` default.
3. Live satellite STT p95 latency DURING a batch run (threshold: ≤ 2× baseline).
4. **Auto-match gate:** per-cluster ECAPA separation on meeting-length audio
   (same-speaker − different-speaker p95 ≥ 0.15, ONNX space). Fail → auto-match is
   not built; pseudonyms + human labeling remain the product.
5. Capture comparison: phone-in-table-center vs an XVF3800 satellite test recording
   (informs the future satellite-recording phase; satellite capture is OUT of §2).

## Config (all env, dark by default)

`MEETING_TRANSCRIPTION_ENABLED=false`, `meeting_whisper_model`, `meeting_max_duration_h`
(default 4), `meeting_keep_audio=false`, `meeting_audio_grace_days`, retention defaults.
Per-instance posture = env defaults only (auto-match to enrolled speakers is not built
and not planned as of 2026-09-20 — both instances: pseudonyms + consent UX,
never-enrollable external participants are expected).

## Explicitly NOT in §2

- Satellite recording ("Renfield, starte Meeting-Aufnahme") — own later phase (needs
  recording indicator/§6 UX, Pi storage/streaming, new WS messages).
- Minutes pipeline (summary/decisions/action-items with human confirm) — later phase;
  Schicht-A stays gated off for transcripts until then.
- Chunked processing: **chunking is built** (#1011, voice-server `meeting_chunk_seconds`,
  `meeting_service.py` — bounded GPU peak, multi-hour recordings); **per-chunk
  checkpoints (resume after a crash mid-meeting) are not built** — escalation path if
  that ever bites.
- Project model/timeline (phase 1 of the instance plan), Notes feature.

## Test plan

See the eng-review test plan artifact (22 traced paths, 0 pre-existing coverage —
all tests ship in the same PR per TDD rule): route gates (flag/format/size/consent/
ownership), worker idempotency (redelivery on completed = ack+skip; in-flight guard),
retry-after-ingest-failure, poison-pill quarantine, margin/pseudonym matcher tests,
alignment fixtures (overlap/gaps, no GPU), migration via real alembic upgrade,
staging E2E incl. live-latency measurement during batch.

## Background moved from CLAUDE.md (2026-09-20)

As-built summary of what the design above became, as it was recorded in CLAUDE.md. The editing invariants now live
in `.claude/rules/meetings.md`.

**Flags and spike gate**

- `MEETING_TRANSCRIPTION_ENABLED` (backend) / voice-server `MEETING_ENABLED`, dark by default in the config.
- Spike-gated build: the `tests/eval/diarization/gates.yaml` gates PASSED on Blackwell on 2026-07-14; harness
  `bin/run_diarization_eval.py`.

**Flow as built**

- `POST /api/meetings/transcribe` — consent REQUIRED (else 422); multi-hour audio streamed chunk-by-chunk to the
  shared uploads PVC; 202 `{id}`.
- Optional per-meeting `language` = `auto` / ISO code, stored on `Meeting.language` (migration `pc20260722c`) and
  threaded worker → `/transcribe-meeting` → whisper. The meeting ASR path used to hardcode
  `whisper_language_default=de`, so English meetings came back as hallucinated German. The UI defaults to
  `auto`-detect for the mixed EN/DE customer base.
- A `Meeting(status=pending)` row (`meetings` table, migration `pc20260714`) → `MeetingTaskQueue` (own Redis stream
  `renfield:tasks:meeting`) → the meeting worker (`workers/meeting_worker.py`, `k8s/meeting-worker.yaml`, replicas:1).
  The worker clones the document worker and adds a row-level `status` + `heartbeat_at` in-flight guard for the
  multi-hour job, poison-pill quarantine, and a 4xx-terminal / 5xx-retryable `VoiceServerError` split so a corrupt
  recording fails fast instead of re-burning the GPU.
- Voice-server `POST /transcribe-meeting` (`voice-server/voice_server/services/meeting_service.py`): pyannote
  diarization + faster-whisper word-timestamps + a PURE fixture-tested `align_words_to_segments` + per-cluster ECAPA in
  the ONNX `/stt` space. pyannote loads only when `MEETING_ENABLED`; the image bakes GPU torch cu128 + the pyannote
  model via a BuildKit secret.
- Attribution = honest pseudonyms ("Sprecher N") + one-click human labeling (`POST /api/meetings/{id}/relabel`,
  re-render → reindex in place, stable `transcript_document_id`). Auto-match to enrolled speakers is NOT
  built and not planned (decision 2026-09-20; the placeholder flag `meeting_auto_match_enabled` was removed) —
  the spike separation gate was insufficient-data on synthetic audio; anonymous cross-meeting fingerprints cover the need.
- Ingest into a dedicated "Meetings" KB via `folder_ingest.ingest_document` with `source="meeting_transcript"` (new
  `documents.source` column, migration `pc20260714b`), which gates Schicht-A OFF (D14) and sets
  `file_to_paperless=False`.
- Biometrics: the voice-server's per-cluster ECAPA `embedding` is never stored on `Meeting.segments`
  (`meeting_pipeline.strip_biometric_fields` / `_set_segments`, the only segments write path; re-render and relabel
  clean legacy rows; `GET …/segments` filters them). Legacy rows: `bin/purge_meeting_segment_embeddings.py`
  (`--dry-run` / `--commit`, counts only).

**Retention**

- `retention_until` is stamped at upload from `meeting_retention_days`.
- A daily job (`services/meeting_retention.py`) purges expired transcripts (via the document-delete path) + segments
  + audio, and grace-cleans completed/failed meetings' audio (`meeting_audio_grace_days`; `meeting_keep_audio` opt-in).

**Routes**

- `POST /transcribe`, `GET /{id}`, `GET /{id}/segments`, `POST /{id}/relabel`, `DELETE /{id}` under `/api/meetings`,
  plus the added `GET /api/meetings` list. Reading follows tier REACH since auth-on §8.1 (the tier said
  "shared artifact" from the start while the routes filtered on owner equality); every MUTATOR stays
  owner-bound via `_get_owned_meeting(..., for_write=True)`.
- `DELETE /{id}` is an owner-gated whole-meeting delete — transcript doc + audio + row — via the shared
  `meeting_retention.purge_meeting` cascade the retention sweep also uses (UI: a trash button + inline confirm on
  each card).

**Frontend**

- `pages/MeetingsPage.tsx` + the dedicated `pages/MeetingDetailPage.tsx` at `/meetings/{id}` (PR-3 / Track D),
  flag-gated on `meeting_transcription_enabled` from `/api/config/features` → nav + route absent when off.
- List page = upload form (mandatory consent checkbox) + a status list that polls only while a meeting is
  pending/processing. A completed card is a LINK to its detail page (no inline expand).
- Detail page = the deliverable-first surface: minutes (summary/decisions/action-items) as the default view up top,
  the raw transcript secondary + collapsed below, a draft-confirm nudge banner, per-speaker relabel, project link,
  delete, and a deep-link to `/knowledge?doc=`.
- Shared building blocks in `components/meetings/` (`StatusBadge` / `TranscriptView` / `MinutesPanel` /
  `ProjectSelect`), consumed by both pages; `useMeeting(id)` → `GET /api/meetings/{id}`.

**Rollout status and the two post-launch fixes**

- LIVE on both the household (`renfield`) and xidra — flag flipped + browser-E2E verified.
- Two operational fixes shipped after the first production upload on xidra:
  - The frontend upload call sets `timeout: 0`. The shared `apiClient` otherwise aborts a several-hundred-MB
    recording client-side at its 30s default (#1009).
  - The voice-server GPU-OOM root cause was ECAPA, not whisper: a meeting feeds a speaker's WHOLE concatenated audio
    to the embedding, whose onnxruntime arena retains the peak. `speaker_service.cap_clip` bounds the ECAPA input to a
    centered 30s window (`speaker_embed_max_seconds`, voice-server v0.3.6, #1012; verified on repeated 32-min
    recordings). Chunked transcription (`meeting_chunk_seconds`, v0.3.5) stays as a backstop for pathologically long
    recordings.
- Related: the voice-identity design plans STREAMING diarization — this §2 batch integration is the shared
  pyannote/cu128 image layer it must reuse, not duplicate.
