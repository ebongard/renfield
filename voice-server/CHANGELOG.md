# voice-server changelog

Releases via `bin/release-voice-server.sh` — the script refuses to release a
version without a section here. Pushed digests live in `RELEASES.md`.

## [0.3.4] — 2026-07-20

- **Meeting transcription: free the CUDA cache between diarization and ASR.**
  `MeetingDiarizationService.transcribe` ran pyannote (torch) then faster-whisper
  (CTranslate2, a SEPARATE CUDA pool). On a busy shared GPU, pyannote's torch
  caching-allocator memory left no room for whisper's `encode`, OOM-ing a ~32-min
  recording (`CUDA failed with error out of memory`) even though the diarization
  tensors were already dead. Added `torch.cuda.empty_cache()` after diarization
  (before whisper) and after each job, so the cache doesn't starve whisper or the
  other services sharing the GPU. Best-effort / CUDA-only. Pairs with
  `PYTORCH_CUDA_ALLOC_CONF=expandable_segments:True` on the deployment.

## [0.3.3] — 2026-07-19

- **One-shot decode reads a seekable file, not a stdin pipe.** `/api/voice/stt`
  and `/transcribe-meeting` streamed the whole upload into RAM and fed it to
  `ffmpeg -i pipe:0`. A pipe is not seekable, so a normal phone/Mac **m4a**
  (AAC in MP4 with the `moov` index atom at the END of the file, past ffmpeg's
  ~5 MB probe window) decoded to zero PCM — `offset 0x2c: partial file /
  Invalid data` — and failed every meeting uploaded from an iPhone/Mac. The
  one-shot decoder now streams the upload to a seekable temp file in fixed-size
  chunks and runs `ffmpeg -i <file>`; MP4/m4a decode correctly and a multi-hour
  meeting no longer sits whole in RAM on the media layer. Every other container
  (wav/webm/opus/mp3/flac) is unaffected — they decode from a file just as well.
  New `decode_upload_to_pcm` (preferred, chunked-stream entry); `decode_audio_to_pcm`
  kept for byte-callers, now also file-backed. `VOICE_ONESHOT_SPOOL_DIR` steers
  the temp file onto a disk-backed volume (keep multi-hour spools off a tmpfs).
  Regression test synthesizes a >6 MB moov-at-end m4a. Streaming WS path
  (`audio_decoder.py`) is unchanged (live chunks are inherently non-seekable).

## [0.3.2] — 2026-07-19

- **`anon_default_client`** (household migration T31): on the anon listener
  only, a request with no `X-Voice-Client` defaults to the configured client
  id (must be an `anonymous: true` row). Lets a caller that predates the
  X-Voice-Client header (the renfield household backend) use the shared
  instance via the NetworkPolicy-fenced anon port without a rebuild. Empty =
  off; never applies on the primary port.

## [0.3.1] — 2026-07-18

- **X-Verify-Secret header** (extraction plan T11): registry rows gain an
  optional `verify_secret`, and `callback` mode an optional
  `AUTH_CALLBACK_SECRET`; when set, voice-server sends the secret as the
  `X-Verify-Secret` header on the verify POST. The client backend gates its
  (unauthenticated-by-design, now cross-cluster-reachable) verify endpoint on
  it — closes the token oracle without depending on source-IP allowlisting
  (prod Traefik is externalTrafficPolicy: Cluster, so an IP allowlist would
  match SNAT'd node IPs). Opt-in; unset = no header (backward compatible).

## [0.3.0] — 2026-07-18

- **AUTH_MODE=registry**: one shared instance authenticates N client products.
  `AUTH_CLIENTS` maps client-id → `{verify_url}` XOR `{anonymous: true}`;
  REST callers send `X-Voice-Client`, WS `?client=`. Verify contract pinned to
  `{user_id}`; payloads namespaced by `client_id`. Anonymous rows are honored
  only via the dedicated `ANON_PORT` listener (NetworkPolicy-fenced by the
  deployment). Fail-closed on empty registry. `local`/`callback` unchanged.
  (#987)
- **Fix**: latent NameError in `/ws/voice` — the M4 session-cap referenced
  `settings` without importing it, so every authenticated WebSocket connect
  crashed (live-confirmed HTTP 500 on the v0.2.0 handshake). (#987)
- **Fix**: the anon listener no longer captures process SIGTERM/SIGINT
  (uvicorn ≥0.30 registers handlers unconditionally in `serve()`; a second
  in-process Server stole the primary's handlers and broke graceful
  shutdown), and a bind failure on `ANON_PORT` now aborts startup loudly
  instead of dying as an unretrieved task exception. (#987 review)
- **Fix**: transport-failure auth errors no longer leak the internal verify
  URL to callers via 401 detail / WS close reason. (#987 review)

## [0.2.0] — 2026-05 (retroactive)

- Meeting diarization (`/transcribe-meeting`, pyannote 3.1 + word timestamps,
  `MEETING_ENABLED`), Opus satellite path (`/api/voice/stt-opus`), session cap
  (`MAX_CONCURRENT_SESSIONS`), opus decode-amplification guard, fail-closed
  placeholder-key check. Shipped with `__version__` still reading 0.1.0 —
  the drift this changelog + release script exist to prevent.

## [0.1.5] — 2026-05 (retroactive)

- Baseline STT/TTS/speaker image as deployed for Reva on cuda.local
  (`AUTH_MODE=callback`).
