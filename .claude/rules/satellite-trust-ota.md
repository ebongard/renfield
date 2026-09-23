---
paths:
  - "src/satellite/renfield_satellite/update/**"
  - "bin/sign_satellite_release.py"
  - "src/satellite/RELEASE_MANIFEST.json*"
  - "src/backend/ha_glue/services/satellite_enrollment*"
  - "src/backend/ha_glue/services/satellite_update_service.py"
  - "src/backend/ha_glue/api/routes/satellite_enrollment.py"
  - "bin/enroll_satellite.py"
---
# Satellite trust: signed OTA (H6) + enrollment credential (H1)

Loaded only when OTA-signing / update / enrollment code is read. Long form: `docs/SATELLITE_OTA_UPDATES.md`,
`docs/ENVIRONMENT_VARIABLES.md` (staged rollout + break-glass), `docs/private/security/satellite-trust-design.md`.

## Signed OTA — code authenticity is independent of transport/backend trust
- Signed = a canonical JSON manifest: `version` + sorted per-file SHA256 of `renfield_satellite/**` (excl.
  `__pycache__`/`.pyc`) plus `requirements.txt`/`setup.py`/`pyproject.toml`, Ed25519. A source manifest, because the
  backend builds the tarball dynamically — bytes built on demand cannot be pre-signed.
- **Every satellite source change needs a version bump (`__version__`) AND a re-sign**
  (`bin/sign_satellite_release.py --sign`, check with `--verify`), committing `src/satellite/RELEASE_MANIFEST.json` +
  `.sig`. The fleet is fail-closed: a stale manifest fails the tree-hash check, so every OTA aborts. Without a bump no
  update is ever attempted and the breakage stays invisible.
- **The private key lives only on the operator workstation** (`~/.renfield/ota_release_key`) — never on the backend
  or build box, never in git. Public keys (`satellite_release_pubkeys` in group_vars → `update.release_pubkeys`, N keys
  = rotation) are safe in git.
- Backend (`satellite_update_service`) only FORWARDS manifest + signature verbatim in the `update_request`; it cannot
  mint a signature. A half-present pair (manifest xor `.sig`) is refused, never treated as "unsigned".
  `satellite_ota_require_signature` (config default off) refuses to push an unsigned release. The tarball excludes
  `__pycache__`/`.pyc`.
- Satellite (`update_manager._verify_signature`) runs after extract, BEFORE install: signature over the manifest bytes
  against the pinned keys → `manifest.version == target_version` → recomputed tree hashes (tamper/injection/missing).
  `update.require_signature` = fail-closed; otherwise verify-if-present — unsigned legacy installs on the checksum, but
  a present-but-invalid signature ALWAYS aborts. The OTA checksum stays an integrity check only.
- **`SAFE_PACKAGES` lives in the RUNNING code, not in the package.** It must match `src/satellite/requirements.txt`
  (`tests/satellite/test_update_security.py::test_shipped_requirements_pass_validation` — a red run there is a real
  bug). A device below the version that adds an allowlist entry can never OTA to that version: its old installer
  rejects the new dependency and rolls back (#1210) → it needs a manual deploy. Names compare after PEP 503
  canonicalisation; never delete separators (look-alike PyPI project = OTA RCE bypass).
- **OTA cannot update the k8s-pod satellite** (code is in the image layer) — rebuild the image, see `satellites.md`.

## Enrollment PSK (`satellites` table, migration `pc20260624`)
- Per-device 256-bit PSK, stored only as a bcrypt hash; presented in the register frame's `token` field, verified
  constant-time. Mint: `bin/enroll_satellite.py <satellite_id>` or the ADMIN-gated `/api/satellite-enrollment` UI
  (token shown ONCE). Provision via the gitignored host_var `satellite_enrollment_token` or a per-pod k8s Secret
  (`RENFIELD_ENROLLMENT_TOKEN`, mounted `optional: true` so the pod still boots dark).
- **Handshake credential (`SATELLITE_PSK_HANDSHAKE_ENABLED`, dark; auth-on cutover D-4c):** the same PSK is also
  accepted at the WS handshake as `Authorization: Bearer sat.<satellite_id>.<secret>` — self-identifying, so exactly
  one row is bcrypt-verified (off the event loop, behind the connection limiter). Lockout per (satellite_id, IP) runs
  BEFORE the compare **only when `TRUSTED_PROXIES` makes the address spoof-resistant** — never keyed on the id alone
  (that would be a 5-guess LAN DoS against a guessable room slug); admin unlock `POST /api/satellite-enrollment/{id}/unlock`.
  A `sat.` token in the URL is refused (header, or cookie, only); a failing `sat.` token never falls through to
  JWT/device-token; independent of the enrollment GATE (always verifies); binds only the satellite_id — the register
  frame must name the same id (`identity-mismatch` → 4001). A PSK-authenticated handshake **satisfies the register
  gate when the frame carries no token** (one secret, provisioned once); a presented token is still verified.
  **Only `/ws/satellite` accepts it** (`authenticate_websocket(..., allow_satellite_psk=True)` — every other WS
  endpoint refuses a `sat.` token outright; never pass the flag elsewhere).
  No user is bound (device account = P0 Nr. 3). Under `AUTH_ENABLED=false` the header is never read.
- **The device half is a separate half, and it ships FIRST.** A satellite that sends no `Authorization` header is
  403'd before the register frame; after the flag flips, OTA travels over that very link, so only Ansible is left.
  Three traps, all live-verified 2026-09-23: the satellite DERIVES `sat.<id>.<psk>` from its own enrollment token
  (`_fetch_and_set_token`; an explicit `server.auth_token` still wins); the gate is `_needs_handshake_credential()`
  = `auth_enabled OR enrollment_token`, never `auth_enabled` alone (False on every device provisioned auth-off);
  the header kwarg comes from `websockets.connect`'s SIGNATURE (`_HEADERS_KWARG`) — `extra_headers` became
  `additional_headers` in 14 and raises a TypeError in 16.x that `connect()` swallows, so the wrong name reads as
  "cannot connect" forever, and the fleet runs several websockets versions.
- Modes: OFF (`SATELLITE_ENROLLMENT_ENABLED=false`, the code default: register path byte-identical to legacy, IRK push
  on `SATELLITE_IRK_ALLOWLIST`) → **PERMISSIVE** (a presented PSK is verified; wrong/unknown/revoked rejected; no token
  allowed-but-logged) → **ENFORCING** (no valid PSK → reject).
- PERMISSIVE→ENFORCING is an auto-flip with a **persisted latch** (`satellite_fleet_state`, gated
  `SATELLITE_ENROLLMENT_AUTOFLIP_ENABLED`): it flips once EVERY enrolled row has AUTHENTICATED at least once (not
  merely connected) and **never auto-clears** — a later enrolled-but-offline satellite must not re-open the fleet.
- **IRKs go only to authenticated satellites:** both push paths (`presence_service.irks_for_satellite` +
  `push_macs_to_satellites`) key on `SatelliteInfo.authenticated` when enrollment is on. IRKs are location-tracking keys.
- The eviction guard never lets an unauthenticated newcomer evict an enrolled incumbent. `/api/ws/token` 401s an
  unauthenticated caller when auth is on.
