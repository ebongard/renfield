# Plan — Provenance chips in chat (roadmap item 7)

Source: `docs/design/chat-ui-modernization.md` Tier 1 item 7. First-slice build.
Goal: show **which sources** a knowledge-backed chat answer used, as chips under
the assistant turn (filename + access-tier), so the answer's provenance is visible.

## Reality check (verified, corrects the roadmap doc)
- `FactProvenance` is NOT reusable here — it renders deterministic(✓)/advisory(~),
  not document-source chips. Source chips are a NEW small component reusing `TierBadge`.
- NOT pure-frontend: `knowledge_tool.py` flattens `rag.search` results into a
  `context` string and discards per-source structure; the chat stream/`Message`
  carries no sources today. Needs backend capture + stream + persist.
- Circle-safety: `rag.search(user_id=...)` already circle-filters retrieval, so
  sources only contain rows the asker could already see — no `circle_sql` change,
  no second read path.

## Backend
- [x] `services/knowledge_tool.py` — returns `data.sources`: deduped
  `[{document_id, filename, title, tier}]` from `rag.search` results (tier IS on
  the result: `document.circle_tier`). `context` text unchanged.
- [x] `api/websocket/chat_handler.py` — added `_extract_agent_sources()` helper +
  shared `agent_tool_results` init; on the shared persist/done path, derives
  sources from the turn's `internal.knowledge_search` tool results (deduped),
  attaches to `assistant_metadata["sources"]` (persist) + `done_msg["sources"]`
  (live). NOTE: agent loop runs in chat_handler, NOT agent_service — corrected
  from the plan. Scope v1 = knowledge_search only.

## Frontend
- [x] `types/chat.ts` — added `MessageSource` + `sources?` on `ChatMessage` and
  `ChatDoneMessage`.
- [x] `components/chat/SourceChips.tsx` — NEW. Chip per source: filename +
  `<TierBadge>` (when tier 0-4), links to `/knowledge?doc={id}`. Empty/undefined
  → renders nothing. Dark mode + i18n (de+en `chat.sources.*`). Caps at 6 with
  "+N weitere" expand. Clickable (chosen over labels-only).
- [x] `pages/ChatPage/ChatMessages.tsx` — renders `<SourceChips>` under finished
  assistant turns.
- [x] `context/ChatContext.tsx` — `ChatUiMessage.sources`; `done` handler attaches
  `data.sources`; `historyToUiMessage` rehydrates from `metadata.sources`.
- [x] `hooks/useChatWebSocket.ts` — `DoneMessage.sources`.

## Tests
- [x] Backend: `test_knowledge_tool.py` (structured deduped sources; no-results →
  no key) + `test_chat_handler_provenance.py` (`_extract_agent_sources` dedupe,
  ignores non-knowledge_search, empty/malformed). 11 passed on .159.
- [x] Frontend RTL: `SourceChips.test.tsx` — renders/links, empty → nothing,
  cap+overflow, tier-absent. 4 passed.

## Out of scope (v1)
- Clickable deep-link into `/wissen` (can be a fast-follow; v1 may ship labels).
- document_fact / KG-edge provenance (knowledge_search only for v1).
- Items 2/4/6 of the first slice (separate PRs).

## Review
- Verified the "data exists / pure-frontend" roadmap claim was WRONG before coding:
  `knowledge_search` discarded per-source structure; chat stream carried no sources;
  `FactProvenance` is a deterministic/advisory glyph, not a document-source chip.
- Circle-safety: sources come only from `rag.search(user_id=...)` which is already
  circle-filtered — no `circle_sql` change, no second read path.
- Tests green: backend 11/11 (.159), frontend 4/4 (isolated). `tsc` clean on the 5
  changed files (2 pre-existing errors in untouched skills.ts/trajectories.ts).
- Pending before deploy: `npm run build` (prod Tailwind pass — per the frontend
  prod-build gate), and `/review`.
