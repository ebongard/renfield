# voice-server changelog

Releases via `bin/release-voice-server.sh` — the script refuses to release a
version without a section here. Pushed digests live in `RELEASES.md`.

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
