# Design — Meetings: one confirmed extraction → minutes + KG + commitments

**Status:** APPROVED v2 (external re-review verdict "v2 sound" — all four v1 findings verified closed; five under-costed edges C1–C5 folded in below before Track B/C build). Phase 0 in progress.
**Author:** 2026-07-21.
**Scope:** primarily the auth-on business instance (`renfield-xidra`), where meetings + KG + projects are live. Household ships the same code, lower value.

**Locked decisions (operator, 2026-07-21):**
1. **Extraction source = a single structured, confirmed pass** — not the generic per-chunk document hook on raw ASR.
2. **Speaker identity = the full solution** — cross-meeting anonymous fingerprints + enrolled matching, not enrolled-only.
3. **Commitments = a source-flexible obligation model (3b)** — not the document-fact substrate.

---

## Why

Meeting transcripts are a **high-value** source (a single working session yields real org/systems/people structure), but the current path has three defects: no cross-meeting speaker identity (pseudonyms collide + leak as junk entities), transcript noise in the graph, and the confirmed **status + action-points dead-ending** in a JSON blob (D14 muted the human-confirmed output along with the raw small talk). And the value that *is* produced — the minutes — is buried in the UI so drafts rot unseen.

## The reframe (the core architectural decision)

**Stop treating a meeting as "a document to run the generic ingest hooks on."** Today a transcript is ingested like any document, so the generic RAG + **KG** + Schicht-A hooks fire on raw, noisy, pseudonym-labelled ASR. That is the root of the provenance mess, the chunk-vs-turn attribution failure, and the pseudonym leak.

**Treat a meeting as "a conversation to run ONE structured, speaker-aware, human-confirmed extraction on."** A single purpose-built pass — the existing `MinutesExtractor` grown richer — produces one DRAFT object:

```
{
  summary,
  decisions:     [{ text, made_by }],
  action_items:  [{ text, owner, due_hint }],
  entities:      [{ name, type }],
  relations:     [{ subject, predicate, object, stated_by }]
}
```

The human reviews + **confirms** it (the minutes-confirm gate, extended). Only on confirm does anything fan out:
- **minutes** → rendered into the transcript document (as today),
- **KG** → entities + relations, with `stated_by` = the fingerprinted speaker (Track A), tracked with **meeting-scoped provenance**,
- **commitments** → source-flexible dated obligations (Track C).

Two consequences that make the reviewer's hard problems **not exist**:
- The generic **KG document hook is turned OFF for `source='meeting_transcript'`** (mirrors what D14 already does for Schicht-A). The *only* KG a meeting produces is the confirmed structured pass — extracted with speaker context (so `stated_by` is a data field, not a chunk-span guess) and after relabel (so there is no pseudonym era to purge).
- Nothing reaches the KG or the agenda until it is **structured AND human-confirmed**. D14 stays intact for the raw transcript; it is carved out only for the confirmed output.

---

## Corrected substrate reality (honest accounting — v1 over-claimed here)

| Piece | Reality (verified) | Implication |
|---|---|---|
| Meeting segments | **No DB table** — a JSON blob on `Meeting.segments` (JSONB). The per-cluster **ECAPA embedding is persisted inside that JSON** (raw `list[float]`, duplicated per segment), not queryable, not keyed to a speaker. | Track A needs a **new fingerprint table** (halfvec-indexed centroids). This is the hard half, not "wiring". |
| Auto-match | **Unbuilt** — `meeting_auto_match_enabled` is a config flag + a docstring with **zero code references**. `resolve_speaker_from_embedding` is never called from the meeting path. | Track A builds the match path from scratch. |
| `resolve_speaker_from_embedding` | A **live-STT policy wrapper** — enrolled-only under `controlled`, auto-enrolls "Unbekannter Sprecher #N" into the live pool, appends embeddings to profiles (continuous learning), tuned for short utterances. | **Do NOT reuse as-is** (it would pollute the household speaker pool with meeting participants). Reuse the low-level cosine/margin math behind a meeting-specific gate that never auto-enrolls into the live pool. |
| KG relation provenance | `save_relation` dedups **globally** on `(subject, predicate, object)`, keeps a single `source_session_id` = *first* creator. | A per-document delete is unsafe (over- and under-deletes). Meeting relations need their **own** M:N `meeting ↔ relation` provenance so a re-confirm can diff/update only the meeting's contribution. |
| Obligations | Entirely `DocumentFact`-shaped: `document_id` **NOT NULL + CASCADE**, `obligation_date` "AS PRINTED, never computed", agenda requires `obligation_date IS NOT NULL`; facts leak into `internal.knowledge_search` + `/api/atoms`. | 3b: a source-flexible obligation that survives transcript purge, doesn't leak into fact-search, and takes a human-set date. |
| ECAPA embedding space | Produced in the same ONNX `/stt` space as live STT; margin matcher exists. | ✅ compatible — the matching *math* is reusable. |
| Relabel re-runs KG? | **No** — `user_reindex` re-chunks RAG and returns without the KG hook. | Not a problem once KG comes only from the confirmed pass (post-relabel). |

---

## Build tracks

### Track A — Speaker identity / fingerprinting (full)

- **New `meeting_speaker_fingerprints` table:** a stable fingerprint id (anonymous, e.g. "Speaker A1B2") ↔ a persisted ECAPA centroid (`halfvec`, HNSW), optionally bound to a `speaker_id`/person once known. Owner-scoped, circle-tiered.
- **On transcript completion**, per diarized cluster: compute the centroid → (1) match enrolled speakers via the **low-level** cosine+margin (a meeting gate that **never auto-enrolls into the live pool**); (2) else match existing anonymous fingerprints (cross-meeting); (3) else mint a new anonymous fingerprint. Write the resolved identity onto the segment.
  - **SHIPPED — Increment 1 (dark, `meeting_fingerprints_enabled`):** the cross-meeting **anonymous** matching = step (2)+(3). `services/meeting_fingerprint_service.py::resolve_meeting_fingerprints` runs in `process_meeting` after pseudonyms: per-cluster ECAPA centroid → cosine-match owner+**tier**-scoped fingerprints (best ≥ `meeting_fingerprint_match_threshold` [0.60, conservative] AND best−second ≥ `meeting_fingerprint_match_margin` [0.05], else mint), running-mean centroid fold on match, two clusters in one meeting never collapse, `centroid_b64` source-of-truth (sqlite-safe) + pgvector `centroid` kept in sync. Rides `fingerprint_id`/`fingerprint_label` onto segments; display pseudonyms unchanged. 9 service tests. **Step (1) enrolled-speaker auto-match stays deferred** (`meeting_auto_match_enabled`) — Increment 1 makes NO claim about *who*. Matching is Python-side over the small per-owner set; HNSW column is future scale-out.
- **Merge-on-enroll — SHIPPED, Increment 2 (dark):** when a human relabels a cluster in any meeting, `enroll_fingerprint_across_meetings` (`meeting_pipeline.py`, called from the relabel route, best-effort) resolves that cluster's fingerprint, records the confirmed name on it (`person_name`, migration `pc20260722d`), and **back-propagates the name to every OTHER owner+tier meeting sharing that EXACT fingerprint** (relabel + reindex in place). Exact-id only, no fuzzy → prefer split. **`person_name` is owner-scoped ON THE FINGERPRINT, deliberately NOT the global `Speaker` pool** — the `Speaker` table has no owner column + a unique alias, so binding an owner-scoped fingerprint there would conflate two owners' same-named people on the business instance; `speaker_id` stays reserved for a future deliberate voice-profile enrollment. 4 tests. **SHIPPED behind its own sub-flag `meeting_fingerprint_autoname` (dark):** applying a fingerprint's known `person_name` to a FUTURE auto-matched cluster in `process_meeting` — `apply_known_names` overrides the "Sprecher N" pseudonym with the enrolled name when a cluster confidently matches a named fingerprint (`resolve_meeting_fingerprints` now carries `person_name`; a fresh mint is None). SEPARATE flag from `meeting_fingerprints_enabled` because it's the riskier half — a false match becomes a real-name misattribution (worse than a pseudonym), so keep OFF until real-meeting calibration; the human relabel always corrects it and the `fingerprint_id`/`speaker_key` linkage is retained. Conservative default throughout: **prefer split over merge** (a wrong merge silently conflates two people — the magnet-hub failure class).
- **Calibration spike (de-risking gate, do FIRST):** the meeting regime is longer/noisier than live STT; the prior spike stalled on *synthetic* audio. Run a small calibration on **real, consented** meeting audio to set threshold/margin *before* committing the rest of Track A.
  - **RESULT — PASS (2026-07-22).** Ran `bin/run_embedding_separation_eval.py` end-to-end on **public AMI Meeting Corpus** audio (CC-BY 4.0; ES2002/ES2003/IS1000/TS3003 series = 16 distinct participants across 4 groups/rooms, ground-truth segments), embedded through the **live voice-server ECAPA** (`/api/voice/stt`, 192-dim ONNX space — the same model the meeting pipeline uses). 96 intra-person cross-recording pairs vs 1920 inter-person: **intra_mean 0.80 / inter_mean 0.14, margin 0.33 (6.6× the 0.05 gate), EER 0.0018, suggested threshold ~0.48**. A person's cross-meeting centroid is reliably matchable and distinct → the rest of Track A is greenlit. Conservative production operating point ≈ 0.55 (between inter_p95 0.33 and intra_p05 0.66). Manifest built by `bin/build_ami_manifest.py` from AMI RTTMs; run reproducibly via `k8s/ami-embedding-eval-job.yaml`. NOT run on any tenant's live meeting data — public corpus only.
  - **Adjacent — transcription WER baseline (2026-07-22).** `bin/run_transcription_wer_eval.py` scores STT text vs AMI's human word-level transcripts (extracted into the manifest by `build_ami_manifest.py --ami-words-dir --ami-meetings-xml`), isolating ASR from diarization via ground-truth segments. Over the same 16-speaker set: **overall WER 27.5%** (1671 sub / 2458 del / 398 ins over 16 469 ref words) — a credible baseline for AMI Mix-Headset spontaneous multi-party English (deletions dominate = target-speaker words dropped under cross-talk; a per-speaker IHM eval would be lower). **Gotcha:** the voice-server STT defaults to the household language (`de`) and hallucinates German on English audio → the eval must force `--language en` (an un-forced run scored a meaningless 92% WER — a language mismatch, not ASR quality). German household meetings use the `de` path natively.

### Track B — The structured meeting extraction → KG

- **Grow `MinutesExtractor` into a meeting extractor** that emits the unified DRAFT object above (summary/decisions/action_items **+ entities/relations with `stated_by`**), speaker-aware, strict-JSON, human-confirmed.
  - **Single-vs-split pass (decide at build, review C2):** adding entities+relations to the current `{summary, decisions, action_items}` pass couples them into ONE strict-JSON failure domain — today a parse failure fail-closes to `empty_minutes()` (`meeting_minutes.py:154`), so a malformed `relations` field would now also lose the summary/decisions/action-items. **Default: split into two passes** (minutes vs KG/relations) sharing the same speaker-aware context, OR make `_normalize_*` salvage per-section (a bad `relations` array must not null `summary`). Also revisit `_MAX_TRANSCRIPT_CHARS = 24000` (`meeting_minutes.py:37`) — a 2-hour meeting would extract KG only from the head; chunk-and-merge for the KG pass.
- **Turn OFF the generic KG document hook for `meeting_transcript`** (add the `MEETING_TRANSCRIPT_SOURCE` skip to `kg_post_document_ingest_hook`, mirroring `schicht_a_extractor:894`) — but **only once B ships**, so we never have a window with no meeting-KG.
- **On confirm → write to KG** with `stated_by` = the Track-A identity, and record a **meeting-scoped provenance** link (`meeting ↔ relation`, M:N) so a re-confirm diffs/updates only this meeting's relations. This is a *scoped* refcount, not the full global KG-provenance rewrite (that stays a separate, larger item).
  - **M:N delete predicate (specify, review C4):** `save_relation` dedups globally on `(subject, predicate, object)` (`knowledge_graph_service.py:822`), so a meeting's confirmed relation may already exist from a non-meeting document or a second meeting. On re-confirm, a relation dropped by *this* meeting is **hard-deleted only when it has zero remaining provenance** — no other `meeting ↔ relation` M:N link AND no `source_session_id`. Otherwise just remove this meeting's M:N link (the relation still belongs to Document Y / Meeting 2).
- **Noise:** because extraction is a single structured pass over the confirmed conversation (not every chunk), small talk / ASR junk is filtered by construction; the human confirm is the final gate.

### Track C — Commitments (3b)

- **Source-flexible obligation:** obligations gain a polymorphic origin — `document_fact` **or** `meeting_action_item` (nullable `document_id` + `source_kind` + a `meeting_id`/`action_item` ref, or a dedicated `meeting_commitments` table the consumers read via a shared view).
- **Consumers to union (full list — review C1):** agenda (`/brain/fristen`), `obligation_deadline_notifier`, `obligation_calendar_sync`, `.ics` export, `/api/atoms`, **AND the two ledger tables** the notifier + sync own — `obligation_acknowledgements.document_fact_id` (NOT NULL FK, `UNIQUE(document_fact_id, user_id, milestone)`, `database.py:2316` — the fire-once + per-user "Bestätigt" store) and `obligation_calendar_events.document_fact_id` (NOT NULL FK, `UNIQUE(document_fact_id, user_id)`, `database.py:2386`). A `meeting_commitment` has **no `document_fact_id`**, so both ledgers must be re-keyed to a shared `obligation_id` from the unified view (or a polymorphic origin key) — each a migration reworking a NOT-NULL FK + a UNIQUE constraint. Without this the notifier can't fire-once and the calendar sync can't stay idempotent for meeting commitments. **This is the largest hidden cost in Track C — do not underweight it.**
- **On confirm**, each action-item → a commitment: owner = fingerprinted speaker (Track A), **date human-set in the confirm step** (undated → shows on `/projects/{id}` only; dated → also on `/brain/fristen`). No `due_hint`→date parsing (keeps the "never computed" spirit; the human is already reviewing).
- **Re-confirm teardown (specify, review C5 — was open-q #5):** if a confirmed commitment already fired a deadline notification (ledger row) or created a calendar event, and the human then edits/removes it on re-confirm, the diff must **tear down** the ledger row + delete the synced calendar event — not just stop future fires. Compounds C1: the re-keyed ledgers must support retraction, not only insertion.
- **Survives transcript purge** (not CASCADE-bound). Consent fit: the recording can be deleted for privacy while the confirmed commitment is tracked separately.
- Isolated from fact-search (doesn't leak into `internal.knowledge_search`) → D14 stays cleanly carved out.

### Track D — UX (surface the value; TODOS item) — ✅ SHIPPED

- ✅ A **dedicated `/meetings/{id}` page** (`pages/MeetingDetailPage.tsx`) with minutes/decisions/action-items **above a collapsible transcript** — inverts the old hierarchy (deliverable first, raw material second). Completed list cards now **link** to it (no inline expand); shared components extracted to `components/meetings/`; `useMeeting(id)` → `GET /api/meetings/{id}`.
- ✅ A **"Protokoll: Entwurf bereit" badge** on the meeting card links to the detail page, plus a prominent **draft-confirm nudge banner** on the detail page so drafts don't rot.
- Phase 0 (the collapsed-card badge + minutes-above-collapsible-transcript inline) was the first slice; this dedicated page is the full Track D.

---

## Phasing (each hidden migration is its own phase; each behind a flag)

- **Phase 0 — UX + stop-the-bleeding (independent, low risk, now):** Track D surface + strip `Sprecher N` prefixes from the current extraction. Immediate value; **keep** the generic KG hook until Track B replaces it (so meetings keep contributing).
- **Phase 1 — Track A foundation:** the calibration spike **first** (go/no-go gate), then the fingerprint table + matching + merge-on-enroll.
- **Phase 2 — Track B:** the structured extraction pass → confirmed KG with `stated_by` + meeting-scoped relation provenance; turn off the generic KG hook for transcripts *as* it lands. Depends on A for attribution. **Gap-era cleanup (review C3):** every meeting ingested in the Phase-0→Phase-2 window gets chunk-based, pseudonym-labelled relations (`source_session_id="doc:<id>"`, `knowledge_graph_service.py:2021`) with no M:N provenance, which the re-confirm diff can't touch. At cutover, run a one-shot migration to **purge `source_session_id LIKE 'doc:%'` relations belonging to `meeting_transcript` documents** (safe *because* those docs no longer feed the global path) — or explicitly accept + document the residue.
- **Phase 3 — Track C:** the source-flexible commitment model + confirm→commitment + agenda/notifier/calendar union. Depends on B (confirmed action-items) + A (owner).

Track D ships early and in parallel; A/B/C are the dependency chain.

## Open questions / risks

1. **Calibration** — if the Phase-1 spike can't hit a usable threshold/margin on real audio, Track A (and the `stated_by` + owner it feeds) stalls. Everything downstream should degrade gracefully to "unattributed" rather than block. **The A-dependency is therefore SOFT, not hard:** Track B ships KG with `stated_by=null` and Track C ships commitments with a free-text owner if the spike fails — a stalled spike must not block all downstream value.
2. **Cross-meeting merge safety** — false-merge conflates two people. Prefer split; make merge human-confirmable.
3. **Confirm burden** — the whole model hinges on the human confirming. If confirms don't happen, nothing propagates (that's the point, but Track D must make confirm effortless and drafts visible).
4. **Meeting-scoped KG provenance vs the global gap** — this design solves provenance *for meeting relations*; the broader "delete a document's KG relations safely" gap stays open (separate item).
5. **Re-confirm semantics** — editing + re-confirming a meeting must diff its KG relations + commitments (update, not duplicate). Now a specified Track-C sub-task (teardown of fired notifications / synced calendar events) + Track-B M:N delete predicate, not just an open question.

## What this does NOT change
- Schicht-A stays off for raw transcripts (D14 for facts is correct).
- Consent gate, retention job, owner-scoping untouched (commitments deliberately *outlive* retention).
- The generic document/RAG/KG pipeline for non-meeting documents is unchanged.
- Meeting↔project linking (`feat/meeting-project-linking`) is complementary and already built.
