**English** | [Deutsch](CHANGELOG.md)

# Changelog

Notable changes to Renfield since release `v2.6.0`. Follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/); versioning follows [SemVer](https://semver.org/).

For earlier history (v1.2.0 - v2.5.0), see [CHANGELOG.md](CHANGELOG.md) (German only).

---

## [Unreleased]

### Changed
- **Default OCR engine → Tesseract** (#1033). The `force_full_page_ocr` re-run converter (which re-OCRs garbled/scanned documents) now uses **Tesseract** (deu+eng) by default instead of docling-EasyOcr. A 148-document eval over the flagged corpus (`bin/run_ocr_engine_eval.py --all-flagged`) showed Tesseract clearly better: **111/148 improved, only 8 regressed**, quality-gate drop 0.68→0.30, no speed penalty. Switchable via `RAG_OCR_ENGINE` (`Literal["tesseract","easyocr"]`); **fail-safe** — it verifies the tesseract runtime (CLI + deu/eng, or the tesserocr binding) and falls back to EasyOcr if absent, so ingest never crashes. `tesseract-ocr`+deu/eng ship in the backend image. Design/outcome: `docs/design/ocr-engine-eval.md`.

### Infrastructure
- **Harbor push/pull from the home LAN: WAN-hairpin fixed.** Root cause was NOT MTU (0-retransmit re-measurement) but that LAN hosts resolved the registry to the public IP and hairpinned through the WAN upload cap (~72 Mbit/s). Fix: per-node `/etc/hosts` pin to the internal HAProxy path (`192.168.1.1`) → ~200 Mbit/s (100 MB push 11 s → 3 s); idempotent Ansible playbook (`private_k8s/ansible/`). Analysis: `docs/TECHNICAL_DEBT.md` I1 + `public_k8s/docs/harbor-slow-from-home-lan.md`.

### Fixed

Three latent bugs that silently disabled the self-learning system in production — found during end-to-end validation, each fixed, deployed, and verified live:

- **Skill injection threw on every agent turn** (`v2.12.10`) — `SkillService.find_similar` bound `:asker` in a bare `:asker IS NULL` arm; asyncpg aborted the Parse with "could not determine data type of parameter $3". The error was caught as a warning, so no skill (not even a seed) was ever injected into the agent prompt despite `SKILLS_ENABLED=true`. `:asker` is now cast to `INTEGER` at every site; a Postgres-only regression test was added (the sqlite harness runs no server-side Parse, so the bug was invisible there). PR [#676](https://github.com/ebongard/renfield/pull/676).
- **Daily background schedulers never ran on short-lived pods** (`v2.12.11`) — `_spawn_periodic_task` slept one interval before the first `work()`, so a daily scheduler (`interval=86400s`) fired its first tick 24h after boot and the timer reset on every pod restart. Skill Curator, Trajectory Cleanup, Shadow-Log Cleanup, and Speaker Vocab rebuild therefore effectively never ran. An opt-in `run_at_boot` runs one tick right after spawn. PR [#680](https://github.com/ebongard/renfield/pull/680).
- **`agent_trajectories` was always empty and skill auto-extraction never fired** (`v2.12.12`) — the agent loop did not append the terminal `final_answer` step to `context.steps` (only `tool_call`/`tool_result`/`error`). Because the post-turn bookkeeping classifies the turn from `context.steps`, every turn read as `ABORT`: the trajectory was dropped (the default capture-set `success,tool_fail` excludes `abort`) and `turn_success` was always `False` — so no skill auto-extraction, and every injected skill was mis-recorded as a failure (auto-demote risk). The terminal step is now recorded in the `run()` wrapper. PR [#682](https://github.com/ebongard/renfield/pull/682).

## [v2.16.0] — 2026-06-22

### Added

- **Broadcast announcement to all occupied rooms** (`internal.broadcast_announcement`). A public TTS announcement played in EVERY room where someone is currently present — "Ansage an alle: Mittagessen", "tell everyone dinner's ready", "call everyone for dinner". Complements the existing targeted relay (`announce_in_room`); same `presence` role, disambiguated by tool description + prompt, not routing. A shared `_announce_core` opens its OWN DB session (an `AsyncSession` is not concurrency-safe); single-announce synthesizes lazily AFTER the privacy gate, broadcast synthesizes ONCE and fans out semaphore-bounded (cap 4). Public-only: `privacy='personal'` is rejected at the boundary (no N-way vision storm); dedups occupied rooms by `room_id`; resolves canonical names from the `room_id` (presence `room_name` is nullable); honest per-room summary (presence sees only BLE-tracked devices → never claims full coverage). Also fixes a pre-existing bug: the raw-speaker fallback played on only the FIRST speaker in a room — now all. PR [#851](https://github.com/ebongard/renfield/pull/851). Design: `docs/MESSAGE_RELAY.md`.
- **Permission gate for announcements** (`HA_CONTROL`, fail-closed). Both announce tools (`announce_in_room` + `broadcast_announcement`) route TTS into room speakers — a house-wide control action — and are now gated in `ha_glue_execute_tool` (`_HA_CONTROL_GATED_TOOLS`) on the same `HA_CONTROL` permission as `device_action`. `user_permissions=None` (auth disabled OR an unidentified voice turn — no JWT / low-confidence speaker) is allowed, since physical voice presence is its own authorization and "Ansage an alle" must keep working; when a permission list is present (an authenticated user), `HA_CONTROL` is required, else denied BEFORE the tool body runs. So a `Gast` (`ha.read`) cannot drive room or house-wide TTS, while `Familie`/`Admin` (`ha.full` ⊇ `ha.control`) can. Reuses `HA_CONTROL` = no migration. PR [#852](https://github.com/ebongard/renfield/pull/852).

## [v2.8.1] — 2026-05-22

### Changed

- **Vision tier enabled** — `OLLAMA_VISION_MODEL` is now set to `qwen3-vl:8b` (previously empty = disabled). Image queries from satellite cameras are answered again. The model runs on the in-cluster `ollama` pod (k8s-gpu-1, RTX 5060 Ti); a config-only change with no new image. PR [#604](https://github.com/ebongard/renfield/pull/604).

## [v2.8.0] — 2026-05-22

Voice barge-in: the user can interrupt the assistant's speech by simply talking over it (Fork A — acoustic). A Phase 0 measurement confirmed up front that the browser's echo cancellation reliably separates the assistant's own TTS from the user speaking (6.8× margin on laptop speakers). PR [#601](https://github.com/ebongard/renfield/pull/601).

### Added

- **Acoustic barge-in listener** (`useVoiceStream`) — during TTS playback the browser opens an analyser-only mic stream. Sustained voiced energy above threshold cancels playback; the same audio track is promoted to the recorder, so the interrupting utterance is captured as the next query — no second `getUserMedia` call ([#601](https://github.com/ebongard/renfield/pull/601)).
- **`cancelled` acknowledgement frame** in the voice-server `/ws/voice` protocol — a client-initiated cancellation is acknowledged cleanly instead of surfacing as a chat error bubble; an internal cancellation keeps the `error` frame ([#601](https://github.com/ebongard/renfield/pull/601)).
- **`computeRms` / `detectBargeIn`** as pure, tested helpers in `voiceAudioUtils.ts`; the existing end-of-utterance VAD now shares the same RMS implementation.

### Fixed

- A client-cancelled TTS request previously raised an error bubble in chat because the voice-server did not distinguish a client cancel from a genuine failure.

## [v2.7.1] — 2026-05-20

### Fixed

- LED and microphone capability badges are now pluralised correctly ([#596](https://github.com/ebongard/renfield/pull/596)).

## [v2.7.0] — 2026-05-19

Pluggable auth-provider registry plus truthful satellite hardware reporting.

### Added

- **Pluggable auth-provider registry + `post_authenticate` hook** — `/auth/login` is no longer hard-wired to "the `authenticate` hook, then bcrypt"; it delegates to a priority-ordered, multi-active provider registry. Built-ins: `db` (bcrypt), `ldap`, and the `google`/`github`/`apple` redirect providers (disabled by default) ([#592](https://github.com/ebongard/renfield/pull/592)).
- **Real per-device satellite hardware reporting** — satellites report and display their actual hardware instead of static assumptions ([#594](https://github.com/ebongard/renfield/pull/594)).

### Fixed

- Closed the enviro pHAT provisioning drift; `python3-smbus` is provisioned and linked into the non-system-site venv ([#593](https://github.com/ebongard/renfield/pull/593)).
- Fixed the `num_leds` template gap; the physical microphone count is now counted truthfully ([#595](https://github.com/ebongard/renfield/pull/595)).

## [v2.6.1] — 2026-05-18

Stabilises the Mem0-v2 memory architecture landed in v2.6.0, plus several chat-UI features.

### Added

- **`build_assistant_card` hook** — new synchronous hook event, the `card_emit_inline` flag, and the `chat_handler` call site for it ([#584](https://github.com/ebongard/renfield/pull/584), [#585](https://github.com/ebongard/renfield/pull/585)).
- **Per-message citation chips** — rendered from each message's persisted metadata ([#587](https://github.com/ebongard/renfield/pull/587)).
- **Wissensbasis panel** — mirrors the left-nav hamburger menu for the Wissensbasis panel ([#588](https://github.com/ebongard/renfield/pull/588)).
- **Lane C — two-stage retrieval** (WIP) with a recency-aware rerank ([#582](https://github.com/ebongard/renfield/pull/582)).

### Changed

- The v2-extract retrieval threshold is now separate from the chat threshold; `MEMORY_EXTRACT_RETRIEVAL_MODE` is deprecated and warns ([#583](https://github.com/ebongard/renfield/pull/583)).

### Fixed

- Leaked advisory locks are released at pool check-in; KG entity/atom creation is atomic ([#578](https://github.com/ebongard/renfield/pull/578)).
- Corrected KG entity/atom ordering and a log-format bug ([#579](https://github.com/ebongard/renfield/pull/579)).
- `retrieve()` now flushes instead of committing — committing broke the shadow savepoint ([#580](https://github.com/ebongard/renfield/pull/580)); the shadow-log row is persisted with an explicit `db.commit()` ([#581](https://github.com/ebongard/renfield/pull/581)).
- Decoupled the k8s init dependency-gates from the scaled-to-0 `llama-server-*` deployments ([#586](https://github.com/ebongard/renfield/pull/586)).

### For contributors

- Unit test for `historyToUiMessage` (the per-message chips mapper) ([#589](https://github.com/ebongard/renfield/pull/589)); added the missing `partialText` to the `defaultChatContextValue` mock ([#590](https://github.com/ebongard/renfield/pull/590)).

## [v2.6.0] — 2026-05-14

Phase 0 baseline-measurement substrate plus Lane B/2 of the Mem0 memory architecture: replaces v1's per-turn LLM loop (which produced a ~91% duplicate rate) with a batched ADD/UPDATE/DELETE/NOOP extractor gated by optimistic concurrency. Architecture is landed and flag-gated. Phase A shadow rollout collects v1/v2 comparison data in `memory_v2_shadow_log` before the `memory_extraction_v2_authoritative=True` flip ([#577](https://github.com/ebongard/renfield/pull/577)).

Empirically validated on cuda.local with qwen3.6:latest: 9/9 CI eval cases PASS, 150-turn corpus shows dedup duplicate rate 91.3% → 0.0% (−91.3 pp, plan-cited target ≤ 1%), pure_add ADD rate 93.8% → 96.9% (+3.1 pp, beats v1), within_turn_contradiction unchanged at 17.4% (retraction-marker detection in the prompt; sub-category split blunt_retraction vs hedged_refinement shows v2 catches the blunt retractions v1 missed). cross_session_stale UPDATE detection 0% → 14.3% — the gap to the 80% target remains for Lane C (two-stage retrieval ranking).

### Added

- **MemoryOps Pydantic schema (Lane B/1)** — New file `services/memory_ops.py` with `OpType` (str enum: ADD/UPDATE/DELETE/NOOP), `MemoryOp` (per-op validators: ADD requires content, UPDATE/DELETE require target_id, ADD must NOT have target_id), `MemoryOpsList` (RootModel with MAX_OPS_PER_BATCH cap and a no-double-target per-batch constraint). `validate_against_candidates()` is a membership check for drift detection; returns an `id_reject` string (Prometheus-label-safe) instead of a bool so future reject reasons can be counted as their own labels ([#577](https://github.com/ebongard/renfield/pull/577)).
- **`extract_and_save_v2` pipeline** — Three-phase Mem0-style extraction in `conversation_memory_service.py`: (1) `_acquire_user_lock` + retrieve top-K candidates, (2) release the lock for the LLM call (prevents lock-hold across multi-second LLM latency), (3) re-lock + drift check + apply ops. Both drift-reject and LLM/schema-reject fall back to v1; the v1 fallback runs OUTSIDE the advisory lock so concurrent v2 calls for the same user don't serialise. Per-user Postgres advisory-lock key uses the `MEM0` namespace (`0x4D454D30`) in the upper 32-bit half ([#577](https://github.com/ebongard/renfield/pull/577)).
- **Retraction-aware v2 extraction prompt** — New YAML keys `extraction_v2_prompt` + `extraction_v2_system` (DE + EN) in `prompts/memory.yaml`. The system prompt establishes ADD as the default action (NOOP requires positive evidence), names retraction markers for within-turn contradictions ("eigentlich nicht", "actually not", "lieber", "stattdessen", "ach nein"), and excludes decision rationale from the personal memory subsystem (atom substrate territory). Mixed-utterance rule: "we're migrating to Postgres because MySQL is bottlenecking" → ADD the plan, drop the rationale ([#577](https://github.com/ebongard/renfield/pull/577)).
- **Phase A shadow-mode substrate** — New `memory_v2_shadow_log` table (alembic revision `q1r2s3t4u5v6`) records both outcomes per turn (v1 committed, v2 rolled back) plus the full `MemoryOpsList` serialisation in `v2_ops_json`. Three indexes (`created_at`, `(user_id, created_at)`, `session_id`) for daily-diff queries. Migration uses `if_not_exists=True` for idempotent re-runs; ORM and migration share `ondelete="SET NULL"` so CI test fixtures (`create_all`) don't diverge from production DDL. Dispatcher (`extract_and_save`) routes between v1 (default), v2-shadow (synchronous comparison + shadow log after v1), and v2-authoritative (v2 primary, v1 fallback) via the `memory_extraction_v2_shadow` / `memory_extraction_v2_authoritative` settings flags. Shadow runs synchronously on the same async session (SQLAlchemy 2 AsyncSession is not concurrent-safe) ([#577](https://github.com/ebongard/renfield/pull/577)).
- **150-turn baseline corpus + harness (Lane A / Phase 0)** — `tests/eval/memory_v1_baseline_corpus.yaml` with 100 Reva + 50 Renfield turns, categorised into pure_add, dedup, cross_session_stale, within_turn_contradiction (with blunt_retraction vs hedged_refinement sub-category), and generic_query. `bin/memory_v1_baseline.py` runs the corpus against either the v1 or v2 extraction (via `--use-v2`), reserves a test-user namespace (≥ 2_000_000_000 via blake2b digest_size=3 so int4 doesn't overflow), and writes per-category aggregate stats. A blocklist prevents accidental runs against prod URLs (`db.reva.example`, `10.0.0.*`, `build-host`, `your-registry.example`) ([#577](https://github.com/ebongard/renfield/pull/577)).
- **CI eval runner + fixture (9 plan-locked cases)** — `bin/run_memory_extraction_eval.py` reads `tests/eval/memory_extraction_eval.yaml` and asserts five expect keys per case: `ops` (deep equality), `ops_count_at_most`, `ops_must_contain_op_types`, `ops_must_not_contain_op_types`, `ops_must_target_id_in_candidates`. Mirrors the production gate sequence: `should_extract_memories` runs FIRST (no LLM call if the gate blocks), then `_call_extract_v2_llm`. Case IDs: pure-add, dedup, cross-session-stale, blunt-retraction, hedged-refinement, generic-query, role-injection, wrong-substrate, circle-leakage. Exit 0 when all pass, 1 on failure, 2 on fixture-load error ([#577](https://github.com/ebongard/renfield/pull/577)).
- **Test coverage** — 56 new tests in `tests/backend/test_memory_ops.py` (Pydantic schema, validate_against_candidates, MAX_OPS_PER_BATCH, per-batch no-double-target) + `tests/backend/test_extract_v2.py` (dispatcher routing, gate sequence, v1 fallback paths). 16 new tests in `tests/eval/test_extraction_eval_runner.py` (check_expectations for all five expect keys, summarize_ops, render_report, TestRunOneCase with a fake service stub that locks the gate-block and gate-pass branches). 9 new tests in `tests/eval/test_memory_v1_baseline.py` (corpus schema lint, prod-URL blocklist, aggregate math) ([#577](https://github.com/ebongard/renfield/pull/577)).

### Changed

- **`should_extract_memories` now also scans `assistant_response`** — The injection-pattern blocklist (Stage 1) previously only tested `user_msg`. But the v2 prompt interpolates both verbatim — a poisoned MCP tool result reflected into `assistant_response` was unchecked. Both sides are now scanned; the log message distinguishes which side matched ([#577](https://github.com/ebongard/renfield/pull/577)).
- **`_apply_update_v2` + `_apply_delete_v2` with user_id predicate** — Both methods now take a `user_id` argument and scope the UPDATE WHERE clause to `id == target_id AND user_id == user_id`. Defense in depth: even if the candidate-retrieval layer ever leaks cross-tier (Circles bug, validator bug), the SQL layer closes the cross-user mutation path ([#577](https://github.com/ebongard/renfield/pull/577)).

### Fixed

- **Shadow log savepoint was never committed** — `_extract_v2_shadow_only` opened a nested transaction via `begin_nested()` and flushed the row, but `finally: pass` never released the savepoint. Under asyncpg this leaves the nested transaction in an unresolved state; Phase A measurement was silently broken — shadow rows weren't durable. Fixed with `await log_sp.commit()` plus a mirrored sp.rollback() error path. Surfaced by the subagent review and confirmed by the verification run on .159 ([#577](https://github.com/ebongard/renfield/pull/577)).
- **`v2_ops_json` column was always NULL** — The column intended to store the full MemoryOpsList serialisation for the daily diff report was never populated. New optional `_ops_capture: list[str]` parameter on `extract_and_save_v2`; when passed, the JSON-serialised MemoryOpsList is appended BEFORE the drift check runs (so drift-rejected LLM intents also land in the shadow log). The shadow caller passes a list and writes the first entry into `v2_ops_json` ([#577](https://github.com/ebongard/renfield/pull/577)).
- **`MEMORY_CHANGED_BY_USER` NameError on every v2 DELETE** — `_apply_delete_v2` referenced the constant, but it was missing from the top-level import block. Every v2 DELETE op would have crashed at runtime; not noticed because the eval fixture doesn't exercise DELETE and production hadn't yet run in v2-authoritative mode ([#577](https://github.com/ebongard/renfield/pull/577)).
- **FK `ondelete` drift between alembic and ORM** — Migration declared `ondelete="SET NULL"`; the ORM ForeignKey had no `ondelete=` argument. Any `create_all()`-based test fixture would have emitted a RESTRICT FK that diverges from production — user deletion in CI would fail with IntegrityError. ORM aligned to `ondelete="SET NULL"` ([#577](https://github.com/ebongard/renfield/pull/577)).
- **`session_id` index schema drift** — ORM had `Column(..., index=True)`; the migration created no index. `alembic check` would have flagged it. Added `idx_memv2sl_session_id` to the migration ([#577](https://github.com/ebongard/renfield/pull/577)).
- **Migration index idempotency anti-pattern** — A pre-DDL `inspector` snapshot was used for per-index "already exists?" checks. Switched to `if_not_exists=True` / `if_exists=True` (Alembic 1.7+); idempotent and actually correct ([#577](https://github.com/ebongard/renfield/pull/577)).
- **v1 fallback ran INSIDE the per-user advisory lock** — The drift-reject branch called `_extract_and_save_v1_impl` inside the Phase 3 try block that held the lock. Contradicted the stated design ("drop lock for LLM call") and serialised concurrent v2 calls for the same user across the full v1 LLM latency. Refactored to a drift_reject flag pattern: fall through to release in finally, then run v1 OUTSIDE the lock ([#577](https://github.com/ebongard/renfield/pull/577)).

---
