# Meeting Transcription & Diarization — Design (§2 of the meeting/project workflow)

> Status: **REVIEWED, spike-gated** (/plan-eng-review 2026-07-06, 9 review findings + 12
> outside-voice findings resolved). Generic feature for BOTH instances (household +
> work); flag-gated dark. Business-instance phasing lives in the private instance plan.
> Build order: **Spike first** — no product code before the spike gates pass.

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
   │  commits / NO review-bucket writes / NO reinforcement), behind
   │  meeting_auto_match_enabled (default false — work instance keeps it off).
   │  ▼
   │  segments JSONB on Meeting row (D7) + rendered markdown →
   │  folder_ingest.ingest_document() into the target KB (D6): dedup, chunking,
   │  embeddings, circle tier — but Schicht-A extraction GATED OFF for
   │  source=meeting_transcript (D14: no phantom obligations/calendar events from
   │  small talk; purpose-built action-item extraction comes with the minutes phase).
   │  file_to_paperless=False for transcripts.
   ▼
 GET /api/meetings/{id} — status poll (pending/processing/completed/failed), owner-gated
 GET /api/meetings     — owner-scoped list, newest-first, capped 1-200 (backs the frontend list; added PR-3)
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
(default 4), `meeting_auto_match_enabled=false`, `meeting_keep_audio=false`,
`meeting_audio_grace_days`, retention defaults. Per-instance posture = env defaults
only (household may enable auto-match if the gate passed; work instance: pseudonyms +
consent UX, never-enrollable external participants are expected).

## Explicitly NOT in §2

- Satellite recording ("Renfield, starte Meeting-Aufnahme") — own later phase (needs
  recording indicator/§6 UX, Pi storage/streaming, new WS messages).
- Minutes pipeline (summary/decisions/action-items with human confirm) — later phase;
  Schicht-A stays gated off for transcripts until then.
- Chunked processing with per-chunk checkpoints — documented ESCALATION path if
  reality routinely brings >2h meetings; not built for a weekly workload.
- Project model/timeline (phase 1 of the instance plan), Notes feature.

## Test plan

See the eng-review test plan artifact (22 traced paths, 0 pre-existing coverage —
all tests ship in the same PR per TDD rule): route gates (flag/format/size/consent/
ownership), worker idempotency (redelivery on completed = ack+skip; in-flight guard),
retry-after-ingest-failure, poison-pill quarantine, margin/pseudonym matcher tests,
alignment fixtures (overlap/gaps, no GPU), migration via real alembic upgrade,
staging E2E incl. live-latency measurement during batch.
