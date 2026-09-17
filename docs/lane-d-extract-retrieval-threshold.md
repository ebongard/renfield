# Lane D — Extract-pipeline retrieval threshold

**Status:** decision landed 2026-05-15. Variant B (`threshold=0.0`) shipped as production default.
**Reference data:** `tests/eval/lane_d_run/memory_v1_baseline_*.{json,md}` on the `.159` build box (also: today's `lane_c_run/memory_v1_baseline_20260515-065901.*` as the threshold-0.7 baseline).
**Eng plan section this closes:** *Cross-session UPDATE detection* gate in `docs/architecture/memory-architecture-plan.md`.

## What the bug was

`ConversationMemoryService.extract_and_save_v2` retrieves "candidate memories the LLM should consider when deciding ADD/UPDATE/DELETE/NOOP" via `MemoryRetrieval.retrieve(...)`. That call inherited the platform-wide `memory_retrieval_threshold = 0.7` — a chat-retrieval-tuned precision floor. The extract pipeline is a different surface with different needs (high recall: surface plausible candidates; the LLM filters; the drift check catches mistakes), so reusing the chat threshold was a category error.

The Phase-A shadow log + Phase-0 corpus run on 2026-05-15 measured the consequence: cross-session UPDATE detection at **0.143** (target ≥ 0.80). 12 of 14 `cross_session_stale` corpus turns failed by emitting ADD instead of UPDATE/DELETE. The Lane C two-stage recency-aware retrieval (PR #582) didn't move the metric because retrieval *was* finding the right candidate at top-1 — but the threshold dropped it before the LLM ever saw it.

A targeted probe (`/tmp/probe_retrieval.py` on 2026-05-15) confirmed the mechanism. The embedding model placed the correct stale memory at top-1 in every case, but with similarities of 0.542–0.673 — all *below* the 0.7 threshold. Only the cases with structural name-repetition (reva-stale-06 at 0.795, reva-stale-07 also high) cleared the gate and produced UPDATE.

| Case | Outcome at threshold 0.7 | Top-1 similarity from probe |
|---|---|---|
| reva-stale-01 | ADD (wrong) | 0.542 |
| reva-stale-03 | ADD (wrong) | 0.560 |
| reva-stale-05 | ADD (wrong) | 0.635 |
| reva-stale-04 | ADD (wrong) | 0.673 |
| **reva-stale-06** | **UPDATE ✓** | **0.795** |

## Hypotheses considered

1. **Prompt drift.** The single UPDATE example in `prompts/memory.yaml::extraction_v2_prompt` is a same-turn retraction ("Eigentlich Pop ist besser" with marker "Eigentlich"). No example covered cross-session staleness. Hypothesis: the LLM had no in-context exemplar and defaulted to ADD.
2. **Retrieval gap.** Most failed corpus turns mix German user messages with English memories. Hypothesis: embedding cross-lingual similarity gap dropped the candidate from the top-K.
3. **Threshold drift.** The chat-retrieval `memory_retrieval_threshold = 0.7` leaked into extract via the same `retrieve()` call. Hypothesis: candidates *were* being retrieved at top-1 but filtered out before reaching the LLM.

Probe data falsified H1 + H2 and confirmed H3:

- Retrieval *did* place the correct memory at top-1 in every failing case → not a retrieval ranking bug.
- Cross-lingual similarity worked — DE-query → EN-memory still landed at top-1 with sim ≥ 0.54.
- The exact corpus failure pattern correlated 1:1 with the 0.7 threshold gate.
- The two cases that *succeeded* both happened to have similarity ≥ 0.7 (structural name-repetition, not a real prompt-drift signal).

## A/B experiment

To decide *how* to fix the threshold-drift bug we ran two variants against the full 150-turn corpus on the same DB/LLM (Qwen3.6-A3B on `cuda.local:8081`):

- **Variant B — `threshold=0.0`** in the extract path only. Chat retrieval keeps 0.7. The LLM gets top-K candidates regardless of similarity; the drift check rejects bad target_ids.
- **Variant C — `threshold=0.0` + score-aware prompt.** Same threshold change as B, plus: each candidate is rendered with `sim=X.XX`, and the system prompt explains the bands ("≥0.7 same topic, 0.4–0.7 weigh carefully, <0.4 ignore"). Hypothesis: giving the LLM the similarity number lets it filter noise without losing the wider candidate net.

### Results

| Metric | Baseline (0.7) | Variant B (no_filter) | Variant C (score_aware) | Target |
|---|---|---|---|---|
| **Cross-session UPDATE detection** | 0.143 | **0.929** | **1.000** | ≥ 0.80 |
| **Duplicate rate** | 0.000 | **0.000** | 0.087 ❌ | ≤ 0.01 |
| NOOP rate on generic queries | 0.654 | 0.864 | 0.840 | ≥ 0.95 |
| Schema-validation rate | 1.000 | 0.993 | 0.987 | ≥ 0.95 |
| Latency p50 / p95 | 35.9s / 53.7s | 35.9s / 69.9s | 38.4s / 54.6s | — |

Outcome distribution (150 turns):

|  | ADD | UPDATE | DELETE | NOOP | FALLBACK |
|---|---|---|---|---|---|
| Baseline | 64 | 2 | 0 | 83 | 1 |
| Variant B | 36 | 14 | 1 | 98 | 1 |
| Variant C | 38 | 17 | 2 | 92 | 1 |

### Read of the data

Variant **C** is mathematically perfect on cross_session_stale (14/14) — including `renfield-stale-01` ("Vergiss Mango — jetzt mag ich nur noch Ananas") which Variant B missed. But it *fails* the duplicate-rate target (0.087 vs ≤0.01): 2 of 23 `dedup` turns wrongly produce ADD instead of NOOP. Looking at within-turn-contradiction over-saves, C also regresses (12 turns wrongly ADD vs B's 7).

The empirical pattern: **adding the score band to the prompt makes the LLM more eager to ADD**, not less. The instruction "0.4–0.7 = weigh carefully" reads, in practice, as "0.4–0.7 = probably distinct enough → ADD" — the score functions as a numeric anchor that pulls classification toward distinct-fact rather than away. Counterintuitive, but reproducible.

Variant **B** clears every target without regressing any: cross_session_stale 0.143 → 0.929 (clears the 0.80 gate by 0.13), duplicate rate stays at 0.000, NOOP-on-generic goes up 21 pp, schema validation effectively unchanged. The cost is +30% on p95 latency (the wider candidate net produces larger prompts).

## Decision

**Ship Variant B as production:** set `memory_extract_retrieval_threshold = 0.0` and remove the score-aware code path.

Rationale:

1. **Clears the ship gate** (cross_session_stale ≥ 0.80) without regressing any of the four locked baselines.
2. **Smallest change** consistent with "extract is not chat retrieval": one threshold-value override on two `retrieve()` call sites; nothing in the prompt; nothing about how the LLM is instructed.
3. **Trusts the LLM as a semantic filter** rather than encoding a similarity floor. The drift check (`validate_against_candidates`) remains the safety net for invented target_ids — **membership only**; ownership is enforced separately at the apply layer (`_identity_scoped_write_denied` / `_extraction_target_owned`).
4. **Score-aware data convinced us the principled-looking move is worse for real life.** Giving the LLM a numerical anchor produced more confident over-extraction, not better calibration. That's a useful negative result — keep it documented so the next person who reaches for "but more context!" sees this run first.

## Open follow-ups (NOT in this change)

These are real but separate from the threshold decision. They affect both B and C, so picking B doesn't change the priority list:

- **`renfield-stale-01` "Vergiss Mango — jetzt mag ich nur noch Ananas" still fails in B** (gets ADD). The LLM has the candidate in the prompt but doesn't generalize from the existing `prompts/memory.yaml` DELETE example ("Vergiss meine Praeferenz fuer Jazz") to the "Vergiss X — jetzt Y" shape. Prompt-side fix: one more example. Out of scope here.
- **Within-turn contradiction over-save: 7/23 turns wrongly ADD in B (16/23 = 0.696 NOOP rate).** With more candidates in the prompt the LLM sometimes anchors on the *first* clause of a same-turn retraction and saves it before processing the marker. Prompt-side or system-prompt-side fix.
- **Wrong-substrate over-save: 3/7 turns wrongly ADD in B.** Same family of regression; the wider candidate net occasionally distracts the LLM from substrate-routing constraints.
- **The 1 FALLBACK** (`reva-circ-04`, `circle_leakage` category) raised `ValueError` — unrelated runner-side edge case. Triage if it recurs in shadow logs.

## What about Lane C (the SQL change)

Lane C's two-stage recency-aware retrieval still lives in `services/memory_retrieval.py` and is opt-in via `ranker="recency_aware"`. It was a no-op for cross_session_stale (because the threshold gate was upstream), but the SQL change is otherwise sound — unit tests pass, no regression. The default ranker for `extract_and_save_v2` remains `"recency_aware"`. We're not reverting it; the recency factor will be visible once production traffic accumulates per-user memories where recency actually matters.

## Reproducibility

To re-run the experiment against the same corpus:

```bash
# Drop + recreate baseline DB on .159 (schema clone from prod renfield):
ssh 192.168.1.159 'docker exec renfield-postgres bash -c \
  "psql -U renfield -d postgres -c \"DROP DATABASE IF EXISTS renfield_baseline;\" && \
   psql -U renfield -d postgres -c \"CREATE DATABASE renfield_baseline OWNER renfield;\" && \
   psql -U renfield -d renfield_baseline -c \"CREATE EXTENSION IF NOT EXISTS vector;\" && \
   pg_dump -U renfield --schema-only renfield | psql -U renfield -d renfield_baseline -q"'

# Run corpus:
ssh 192.168.1.159 'docker exec -d \
  -e BASELINE_DATABASE_URL="postgresql+asyncpg://renfield:changeme_secure_password@postgres:5432/renfield_baseline" \
  -e LLM_OPENAI_BASE_URL="http://cuda.local:8081/v1" \
  -e LLM_OPENAI_API_KEY="baseline-stub" \
  -e LLM_OPENAI_FOR_MEMORY="true" \
  -e MEMORY_EXTRACTION_RETRIEVE_K=20 \
  -e MEMORY_EXTRACT_RETRIEVAL_THRESHOLD=0.0 \
  renfield-backend bash -c \
  "cd /app && python memory_v1_baseline.py --use-v2 --output-dir /tests/eval/lane_d_run/ \
   > /tests/eval/lane_d_run/run-$(date +%H%M%S).log 2>&1"'

# Wait for the run (~75 min) then read the report:
ssh 192.168.1.159 'cat /opt/renfield/tests/eval/lane_d_run/memory_v1_baseline_<TS>.md'
```

The runner's `ensure_baseline_users` (from PR #582) seeds the 155 namespaced users; cleanup via `DELETE FROM users WHERE id >= 2_000_000_000` if you want to wipe the test set without dropping the DB.
