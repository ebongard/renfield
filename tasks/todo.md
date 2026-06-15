# Plan — Follow-up suggestion chips (chat-ui roadmap item 2)

Source: `docs/design/chat-ui-modernization.md` Tier 1 item 2. After an assistant
answer, show 2-4 tappable follow-up suggestions under the turn.

## Approach (decided with user, reverses the roadmap's "same generation" line)
Generate in the **single shared seam** in `chat_handler` (after `full_response`,
where every path — conversation / agent / RAG — converges), via ONE small
best-effort call. NO agent-output-contract change. A parse/timeout failure just
drops the chips (answer untouched). Gated + dark by default.

Why not same-generation: a trailing-JSON-in-the-answer block touches every
prompt, is fragile on local models the codebase already distrusts, and a bad
parse could corrupt the visible answer. A dedicated call fails gracefully.

## Decisions
- **Ephemeral, NOT persisted.** Chips are next-step suggestions for the *live*
  turn only → attach to the `done` frame, shown under the LAST assistant turn,
  cleared on the next user send. (Unlike provenance chips, which persist.)
  No `message_metadata`, no history rehydration.
- **Tap = fill the composer + focus** (NOT auto-send) — safer for a household
  (no accidental sends); user reviews/sends. (Speakable-for-voice = follow-up.)
- **Best-effort + bounded** — `asyncio.wait_for(timeout)`, try/except; `done` is
  never blocked beyond the timeout; empty list on any failure. Prose already
  streamed, so only `done`+chips lag by ~one small-model inference.

## Backend
- [ ] `utils/config.py` — `followup_chips_enabled` (False/dark), `followup_chips_model`
  (""→ `ollama_intent_model`), `followup_chips_count` (3), `followup_chips_timeout_seconds` (5).
- [ ] `services/followup_service.py` (NEW) — `generate_followups(user_message, answer,
  lang, *, model, count, timeout) -> list[str]`. Tight prompt → 2-4 short questions
  in the user's language; tolerant parse (JSON array OR newline/bullet lines);
  trims to `count`, drops empties/dupes/overlong; returns `[]` on ANY failure.
- [ ] `chat_handler.py` — in the shared block (after `full_response`, near `done_msg`):
  if `followup_chips_enabled` AND substantive non-error turn (full_response present,
  `action_success` not False, len ≥ small floor), best-effort
  `await asyncio.wait_for(generate_followups(...), timeout)` → `done_msg["suggested_followups"]`.
  Wrapped try/except — never block `done`. (v1: all substantive turns; gate-by-intent = follow-up.)

## Frontend
- [ ] `types/chat.ts` — `ChatDoneMessage.suggested_followups?: string[]`.
- [ ] `hooks/useChatWebSocket.ts` — `DoneMessage.suggested_followups?: string[]`.
- [ ] `context/ChatContext.tsx` — `ChatUiMessage.suggestedFollowups?: string[]`; `done`
  handler attaches `suggested_followups` to the completed msg; ensure only the LAST
  assistant msg carries them (clear/none on older turns is automatic since not persisted).
- [ ] `components/chat/FollowupChips.tsx` (NEW) — tappable chips; tap → `setInput(text)`
  + focus composer (via context). Empty/undefined → renders nothing. 44px targets,
  keyboard-focusable, dark mode + i18n.
- [ ] `pages/ChatPage/ChatMessages.tsx` — render `<FollowupChips>` under the LAST
  finished assistant turn only (index === last && !streaming).

## Tests
- [ ] Backend: `followup_service` parses JSON + line formats → N chips; returns []
  on garbage / timeout / empty; respects `count`. chat_handler attaches when flag on,
  none when off / error turn (mock the service).
- [ ] Frontend RTL: FollowupChips renders chips, empty → nothing, tap calls setInput.

## Out of scope (v1)
- Persistence / history rehydration (ephemeral by design).
- Voice-speakable chips (roadmap noted; follow-up).
- Auto-send on tap; gate-by-intent (generate-for-all is fine behind the flag).

## Status: IMPLEMENTED
- Backend: `config` flags + `services/followup_service.py` (NEW) + `chat_handler`
  shared-seam best-effort call (bounded by `asyncio.wait_for`, try/except, gated).
- Frontend: `types`/`useChatWebSocket` `suggested_followups`; `ChatContext`
  `suggestedFollowups` + done-handler attach; `FollowupChips.tsx` (NEW, tap→setInput);
  `ChatMessages` renders under the LAST finished assistant turn; i18n de+en.
- Parser hardened: requires word content (drops "{}"/"[]"/"---" noise).
- Tests: backend 8 (parse JSON/lines/dedupe/overlong/garbage + generate
  success/empty/best-effort-fail), frontend 7 (FollowupChips 3 + SourceChips 4
  still green). tsc clean on changed files.

## Review
- Reversed the roadmap's "same generation" line with the user — dedicated
  best-effort call fails gracefully (no chips) vs a trailing block that could
  corrupt the answer. Single seam covers all paths, no agent-contract change.
- Ephemeral (done-frame only, last turn) — no persistence/rehydration.
- Pending before deploy: `npm run build` (prod Tailwind) + `/review`.
