---
paths:
  - "src/backend/ha_glue/services/internal_tools.py"
---
# Message relay & broadcast announcements

Loaded only when `ha_glue/services/internal_tools.py` is read. Long form: `docs/MESSAGE_RELAY.md`.

"sag ihm/ihr …" → the agent presence-resolves the person's room, then speaks there via `internal.announce_in_room`.
**LLM-orchestrated — nothing hardcoded.** Targeted relay and broadcast live in the same `presence` role and are
disambiguated by tool description + prompt (`prompts/agent.yaml`), NOT by role routing.

## Privacy gate — FAIL-CLOSED
- A `personal` message airs only if EVERYONE present is an intended recipient (`for_users`); otherwise a neutral
  "message waiting" plus consent-to-`force`. Never air the content first and check later.
- Optional camera occupancy check (`ANNOUNCE_CAMERA_OCCUPANCY_CHECK`): counts people via the vision model to catch
  BLE-untracked bystanders. Backend→satellite `capture_snapshot` WS request; **the snapshot is never persisted**. It
  fires only for `personal` messages whose BLE gate already passed — never for public announcements.

## Permission gate — FAIL-CLOSED on `HA_CONTROL`
Both announce tools are gated in `ha_glue_execute_tool` / `_HA_CONTROL_GATED_TOOLS` (`ha_glue/bootstrap.py`, the same
gate as `device_action`). `user_permissions=None` (auth disabled OR an unidentified-voice turn) is ALLOWED so spoken
announcements keep working; an authenticated low-privilege user (e.g. a `Gast` with `ha.read`) is denied house-wide
TTS. A new announce-like tool must be added to that set.

## Shared core
- Both tools run through `_announce_core(room_name, text, audio_bytes, privacy, for_users, force)`, which opens its
  OWN `AsyncSessionLocal` — an `AsyncSession` is not concurrency-safe, so the parallel broadcast cannot share one.
- Single announce passes `audio_bytes=None` → synthesis stays lazy, POST-gate (a blocked personal message wastes no
  synth). Broadcast synthesizes ONCE and `asyncio.gather`s the core per room behind a semaphore (cap 4).
- The raw-speaker fallback plays on ALL speakers in a room (keyed on the resolved room id), not the first to answer.

## Broadcast (`internal.broadcast_announcement` — "Ansage an alle" / "sag allen" / "ruf alle zum Essen")
- **Public-only:** rejects `privacy='personal'` at the tool boundary (no N-way camera/vision storm).
- Dedups occupied rooms by `room_id` and resolves canonical names from the id — presence `room_name` is nullable.
- Swallows per-room errors and reports the rooms ACTUALLY reached. Presence sees only BLE-tracked devices, so never
  claim full coverage; empty presence → "niemand anwesend" (no synth).
- At-least-once, no idempotency key: the agent is told NOT to retry (a re-fire re-announces in rooms that played).
