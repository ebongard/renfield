# Voicemail transcript — the text of a message in the notification

Status: DRAFT 2026-11-26, architect-reviewed the same day (§11). Issue #1669. Built: step 1 (voice-server `embed=false`, 0.3.13), step 2 (gateway `GET /v1/recordings`, live 2026-11-27), step 3 (Renfield job + loop, dark). Follows the telephony gateway (#1650) and the caller lookup (#1240, `caller-lookup.md`).
Gateway side lives in the private `telefonie` repo (`docs/plan.md` Phase 2b: "Abruf der Audiodatei über den Kanal
(Renfield holt, LAN → DMZ) folgt mit dem Transkript (Whisper) als eigener Schritt").

## 1. Today

- Asterisk answers after 25 s, plays the greeting, records up to 180 s (`Record(… .wav16, 5 s silence, 180 s)`) to
  `/var/spool/asterisk/voicemail-rec/<instance>/<timestamp>-<uniqueid>.wav16` in the DMZ LXC. A timer deletes
  recordings after 30 days.
- A hangup handler raises the ARI UserEvent `TelephonyVoicemail(duration)`; the gateway pushes
  `{"type":"voicemail", call_id, did, caller, duration_s}` to the instance that owns the DID.
- Renfield (`services/telephony_client.py`) dials OUT to the gateway (one WebSocket, `/v1/events`, Bearer token per
  instance, TLS with a pinned CA), and creates one personal notification per active human user:
  "Neue Nachricht von … (0:23)". The audio never leaves the DMZ, so there is no text.

## 2. Decisions taken (user, 2026-11-26)

| Question | Decision |
|---|---|
| Who sees the text? | The same recipients as the voicemail notification (every active human user of the instance). No per-line owner in this stage. |
| Keep the audio in Renfield? | No. Text only; the audio stays on the gateway (30 days). No play button in this stage. |
| Hint in the greeting? | Yes, both lines: "Ihre Nachricht wird automatisch verschriftet." (third-party personal data; xidra is a business line). |

## 3. Invariants

1. **The DMZ never connects into the LAN.** Renfield fetches; no new port, no firewall change.
2. **The gateway hands out only a recording it announced, only to the instance that owns it.** Renfield names a
   recording by an opaque id from the event, never by a path. The gateway resolves the id inside
   `voicemail-rec/<authenticated instance>/` only (no `..`, no symlink follow, regular file, size cap).
3. **The transcript is untrusted third-party text.** Anyone who can dial the number can speak into it. It is never
   read aloud (no TTS), never put into an LLM prompt, never fed to the agent, the KG, memory extraction or RAG in
   this stage. It is shown as text, escaped by React like every notification.
4. **No voiceprint, not even computed.** voice-server `/api/voice/stt` today ALWAYS computes the ECAPA embedding
   (`voice-server/voice_server/api/rest_voice.py` `_transcribe_and_embed`) — dropping it on the client would still
   compute a caller's voiceprint and send it over the wire. So voice-server gets a form field `embed=false`
   (skip the embedding, answer `speaker_embedding: null` + `embedded: false`); it ships FIRST, and Renfield
   refuses the job (status `failed`, no retry) when the reply does not confirm `embedded: false`.
5. **The call notification stays immediate.** Transcription is asynchronous; a failure, a timeout or a gateway
   outage leaves the notification exactly as today ("Neue Nachricht von … (0:23)"). It never retries the event
   and never creates a second voicemail notification.
6. **Content stays out of logs** (`logged_content` / `error_text_without_content`): no transcript, no number.
7. **Dark by default:** `VOICEMAIL_TRANSCRIPT_ENABLED=false`; flag off = byte-identical behaviour, and Renfield
   never requests a recording.

## 4. Flow

```
Asterisk Record → hangup handler → UserEvent TelephonyVoicemail(duration, rec=<basename>)
  → gateway: event voicemail {…, recording: <basename>}                 (buffered, resumable as today)
  → Renfield telephony client: notification per user (unchanged)       ← immediate
  → if flag on: INSERT job (call_id, recording) ON CONFLICT DO NOTHING  ← inside _deliver, before mark_done, never raises
  → backend transcript loop: GET the audio from the gateway (§5) → voice-server STT, embed=false → text
  → update the voicemail notification rows of the original recipients (§7) + content-free /ws/user refresh
```

## 5. Fetching the audio: HTTP GET on the existing listener

`GET /v1/recordings/<basename>` on the gateway's TLS listener, `Authorization: Bearer <token>` (= instance) —
answered in `process_request` (`server.py` `_authenticate`), which already sees path and token; websockets 15
returns a plain HTTP response with a body there. Renfield fetches with `httpx` and the pinned CA
(`telephony_client.ssl_context_for`). No new port, no second protocol.

- **The id is the basename, resolved stateless.** `rec` comes from the dialplan (`${VM_FILE}` basename),
  validated `^\d{8}-\d{6}-[0-9A-Za-z.]+$`; the gateway resolves it as
  `realpath(voicemail-rec/<authenticated instance>/<basename>.wav16)` and serves only a regular file inside that
  directory. No mapping in memory — the event buffer is in memory (200 events, 900 s TTL, `buffer.py`), so a
  mapping would not survive a gateway restart while the job and the file (30 days) do.
- Another instance's basename → 404 (the lookup is confined to the token's directory). Traversal, symlink, a
  non-file → 404. Size > 8 MB → 413.
- **File complete:** the UserEvent fires from the hangup handler after `Record` closed the file; the gateway still
  checks that the RIFF length matches the file size and answers 425 otherwise (the job retries).
- Content-Type `audio/wav` (`.wav16` is a WAV container, 16 kHz mono). TLS carries integrity; Content-Length +
  the streamed size cap on the client cover truncation. No sha256, no chunk framing.
- One fetch in flight per instance (gateway semaphore → 429), plus a small rate limit.

## 6. The transcript job: a table, a loop in the backend

- Table `telephony_voicemails(id, call_id UNIQUE, recording, status pending|done|failed, attempts,
  next_attempt_at, created_at, updated_at)` — a migration, run BEFORE the rollout. The transcript itself is NOT
  stored here (§7). A Redis Stream (`DocumentTaskQueue`) does not fit: it has no `next_attempt_at`, and the job
  must wait for a gateway that may be down.
- **Enqueue** inside `TelephonyClient._deliver`, after the notifications and before `mark_done`, as
  `INSERT … ON CONFLICT (call_id) DO NOTHING` in its own try/except: a replayed voicemail (gateway restart =
  epoch reset, replay from 0) makes no second job, and an enqueue failure is logged and swallowed — it must never
  turn into "delivery failed", which would retry the event and notify everyone twice.
- **Loop** in the backend next to `_schedule_telephony_client` (`api/lifecycle.py`): picks one due row with
  `SELECT … FOR UPDATE SKIP LOCKED` (several backend replicas run the client), processes ONE job at a time,
  a startup sweep picks up pending rows. STT is remote, so the backend only does I/O.
- **Retries:** 3 attempts over ~10 minutes (gateway 5xx/429/425, timeout, STT error); then `failed`, logged
  without content. 404 = file gone → `failed` at once.
- **STT:** `voice_server_client.stt(audio, language="de", embed=False, timeout_s=60)`; the reply must say
  `embedded: false` (§3.4). Download cap and STT timeout enforced while streaming, not afterwards.

## 7. Showing the text: update the existing notification rows

- The rows: `event_type = 'telephony.voicemail'` AND `source_data->>'call_id' = <call_id>` — exactly the users
  that got the voicemail notification, never recomputed. Only rows that are not dismissed and not expired;
  `expires_at` is NOT touched (a 24 h TTL may pass while the job waits — then the text is simply never shown).
- The update sets ONLY `message` (+ `„<text>“ (automatisch verschriftet, kann Fehler enthalten)`), `privacy =
  'confidential'` and `source_data.transcript_status`. It never re-runs `process_webhook`: no second push, no
  feedback-suppression check, and no embedding of the transcript (the row's embedding was computed from the
  text-free message at creation and stays that way).
- The text: control/bidi characters stripped like `caller_lookup._clean`, capped at ~1 000 characters, shown
  as is. A blank result → `transcript_status = 'empty'`, message unchanged.
- Then the content-free `/ws/user` event (`_emit_changed`; reuse a reason the frontend already handles).
- The upgrade to `confidential` cannot recall what was already delivered — fine, that carried no transcript.
- Retention: the notification's own TTL; nothing else keeps the text.

## 8. Greeting

`telefonie/asterisk/greetings/<line>.txt` gets the sentence "Ihre Nachricht wird automatisch verschriftet." before
"Bitte hinterlassen Sie …"; `build.sh` regenerates the audio (Piper, from the LAN) and uploads it.
The hint ships together with switching the flag on, not before.

## 9. Tests

- voice-server: `embed=false` computes no embedding (the model is never called) and says `embedded: false`.
- Gateway: basename → file only within the token's instance dir; the other instance's basename → 404;
  traversal, symlink, non-file → 404; > 8 MB → 413; RIFF length mismatch → 425; a GATEWAY RESTART between event
  and fetch still serves the file; 429 on a second concurrent fetch.
- Renfield: flag off = no job, no request, byte-identical notification (integration test); a replay of the same
  call_id = one job; an enqueue failure leaves the event handled and notifies nobody twice; fetch / STT failure
  and timeout leave the notification untouched; a reply without `embedded: false` = job failed, no text;
  dismissed or expired rows are not updated; only the original recipients' rows change; the row becomes
  confidential; the transcript is not in `source_data`, the embedding or the logs; bidi/control stripped;
  length cap.

## 10a. As built (step 3)

- `services/voicemail_transcript.py`; table `telephony_voicemails`, migration `pc20261127_telephony_voicemails`.
- The claim is a LEASE, not a lock held across the I/O: `FOR UPDATE SKIP LOCKED`, `attempts += 1`,
  `next_attempt_at = now + 5 min`, commit. A pod that dies mid-job leaves the row pending; it runs again when the
  lease is over. Retries after attempt 1 and 2: +2 min, +8 min; attempt 3 failing = `failed`.
- Text and `done` commit in ONE transaction; a row that already carries `transcript_status` is skipped, so a
  repeated job never appends twice.
- The loop is woken by `enqueue` in the same process, otherwise polls every 60 s.
- Found while building: "Ähnliche unterdrücken" embedded `title + message` into a STORED suppression rule. For a
  row with `transcript_status = done` it now embeds the title only (the caller's words never outlive the row).
- `/ws/user` reason `updated` (the frontend refetches on any `notifications_changed`).

## 10. Out of scope (later)

Play button / audio in Renfield; per-line owner; summary or "Rückruf-Erinnerung" by an LLM (needs a design for
untrusted text in a prompt); voicemail as a document in the KB; transcribing call recordings (Phase 5).

## 11. Architect review 2026-11-26 — taken over

HTTP GET instead of a second WebSocket; a table + backend loop instead of a Redis Stream; update the rows (A),
no central store (B dropped). Fixed: the recording id survives a gateway restart (stateless basename); the
voiceprint is not computed at all (`embed=false` in voice-server, verified in `rest_voice.py`); the update never
touches expired/dismissed rows, `expires_at`, the embedding or the push; enqueue is idempotent and can never
retry the event. Dropped for stage 1: sha256/chunk framing, the random id, the "keine verständliche Nachricht"
text, the frontend expand, long backoff.
