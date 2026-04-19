# RAG Parity Plan (parked)

**Status:** PARKED on 2026-04-19, pending worker-split dogfooding period.
**Trigger to resume:** actual household usage reveals retrieval-quality failures
that aren't explained by the async-ingestion migration that shipped hours before
this plan was drafted.

---

## TL;DR

Renfield's RAG stack was compared against five popular OSS RAG systems. The comparison
identified five real gaps. A plan was drafted to close them via "measure first, then
decide" (build an eval harness, baseline current quality, build only the features the
baseline says are actually broken).

The plan was eng-reviewed (scope reduced — we already have `rag_eval_service.py`) and
then externally reviewed. The external reviewer landed one objection that carried the
day: **the timing is wrong.** The worker-split migration literally shipped four hours
before this plan was written. The feedback signal from real household usage against
the new worker has not returned yet. An eval harness built now measures a system that
hasn't stabilized.

Verdict: park the plan. Dogfood the worker-split for a week. If real failures surface,
revisit with evidence instead of a comparison table. If no failures surface, accept
that as the answer — the current system is household-sufficient.

---

## The comparison that motivated the plan

Renfield vs the populated OSS RAG landscape, 2026-04-19:

| | Renfield | RAG-Anything (16k★) | LightRAG (34k★) | RAGFlow (78k★) | LlamaIndex (49k★) | GraphRAG-MS (32k★) |
|---|---|---|---|---|---|---|
| Parser | Docling + EasyOCR | MinerU | pluggable | DeepDoc | 100+ loaders | text-first |
| Vector store | pgvector | pluggable | pluggable | own DB | pluggable | parquet |
| BM25 / FTS | ✓ PG tsvector | — | — | ✓ | via plugins | — |
| Hybrid (RRF) | ✓ k=60 | vector+graph | vector+graph | yes | pluggable | — |
| Reranker | ✓ mxbai embedding | ✓ | ✓ | ✓ | ✓ | — |
| Knowledge Graph | flat triples + scope | multimodal KG | ✓ dual-level | ✓ | via plugins | ✓ hierarchical |
| Multimodal (img/table/math) | caption only | ✓ VLM + LaTeX | text-focus | ✓ tables | OCR agent | — |
| Contextual retrieval | ✓ (just fixed today) | — | — | — | ✓ | — |
| Query decomposition / HyDE | — | via LightRAG | ✓ dual | ✓ | **strong suit** | — |
| Async ingestion worker | **✓ (shipped today)** | batch | depends | ✓ | DIY | batch only |
| Multi-tenant + RPBAC | **✓ 4 role tiers** | — | basic | ✓ | — | — |

Ahead where it matters: BM25+RRF by default (rare in OSS), scope-based KG RPBAC
(unique to us), production ops. Behind where it matters: multimodal VLM,
graph-structured retrieval, query-side sophistication, citation bboxes, incremental
KG. "Where it matters" is doing a lot of work in that sentence — see external
review below.

---

## Gaps identified (in the order the plan originally addressed them)

1. **Multimodal understanding** — no VLM, Docling caption only. RAG-Anything / RAGFlow
   / LlamaIndex have this.
2. **Graph-structured retrieval** — our KG is flat triples + vector entity lookup.
   LightRAG / GraphRAG / RAG-Anything do dual-level or community-based traversal.
3. **Query-side sophistication** — no HyDE, no decomposition, no multi-hop planning.
   LlamaIndex / LightRAG.
4. **Citation / provenance** — Docling returns bbox per chunk, we throw it away.
5. **Batch-only re-indexing** — delete chunks + re-generate. Some systems do
   incremental entity updates.

A sixth gap was identified during office-hours that none of the comparison systems
handle well: **time-aware retrieval**. Three years of Amazon invoices: you usually
want the newest by default. Flat similarity ranking ignores recency.

---

## Plan the office-hours session produced

Three approaches, ordered by risk / scope:

- **A (full plan):** 5 phases, close every gap — bbox → graph → decomposition → VLM
  (gated) → incremental KG. ~6-8 weeks calendar.
- **B (minimal viable):** kill VLM and decomposition and incremental KG entirely.
  Ship bbox + time-aware retrieval (NEW) + graph-retrieval-scoped-to-disambiguation
  + OCR backend upgrade. ~2 weeks.
- **C (recommended at the time):** build eval harness first (2-3 days), baseline
  current system, then decide which phases of B actually earn their place. Data-driven.

Office-hours landed on C. See full office-hours design doc:
`~/.gstack/projects/ebongard-renfield/evdb-main-design-20260419-181300-rag-parity.md`
(external to repo — personal design archive).

---

## Engineering review findings

- **Scope reduction caught:** `src/backend/services/rag_eval_service.py` (188 LOC)
  already exists with 4-dim LLM-as-judge scoring, `data/rag-eval/test_cases.yaml`
  (5 seed queries), admin endpoint, German judge prompts. The plan was about to
  build a duplicate.
- **Plan revised:** extend existing service with `hit@5` retrieval score, N-run
  judge median, variance flag, markdown report, `--sample N` fast-iteration mode.
  Net work drops from ~3d to ~1.5d.
- **Two critical gaps fixed in plan:** YAML `kind` validation (fail fast on
  unknown values), report-path writability check before 10-18 min eval run.
- **13 test coverage gaps** catalogued, 8 concrete unit tests spec'd into
  `tests/backend/test_rag_advanced.py`.
- Review outcome: CLEARED.

---

## External review (Claude adversarial subagent, cold read)

Five objections in severity order. The third, fourth, and fifth are the ones that
parked the plan.

1. **"Measure first" is theater when the premises already pre-ranked outcomes.**
   The plan killed VLM + decomposition + incremental KG before any measurement.
   If the decision is already made, the eval is the work, not the gate.
2. **20 queries from one author = taste test, not baseline.** Author-variance on
   hand-written queries means a ≥10% absolute improvement is 2 queries flipping.
   Well inside noise. Alternative: mine real query logs from conversation history.
3. **`qwen3:14b` judging `qwen3:14b` output is a closed loop.** Self-evaluation
   biases. Median-of-3 fixes variance, not systematic bias. Alternative: different
   model family as judge, or drop LLM-as-judge and lean on objective `hit@5`.
4. **Strategic miscalibration: 9 PRs today on worker-split, eval harness next is
   a detour.** The async ingestion pipeline shipped hours ago. Feedback from real
   usage hasn't returned. Building an eval harness now measures a system that
   hasn't stabilized. Alternative: dogfood the worker-split for a week, log
   failures in a plain text file, THEN decide if an eval is needed at all.
5. **The contextual-retrieval bug (silently broken for weeks, fixed today) is
   the real lesson being ignored.** The gap isn't retrieval quality — it's
   observability. An on-demand eval harness won't catch the next silent
   regression because it runs only when you remember to run it. Alternative: a
   cheap nightly canary (3 queries, known-good chunk IDs, alert on `hit@5`
   drop) catches more classes of failure than the full 20-query harness for
   less work.

External-reviewer's one-line recommendation: *"Kill the baseline-before-feature-work
sequencing — ship date-boost behind a flag this week, use the eval harness only to
tune it, and stop pretending premises that are already decided are open questions
awaiting evidence."*

---

## Why parked (not rejected)

The external reviewer's #4 and #5 carried the day. The worker-split migration is
the actual ongoing signal. Measuring quality now measures a system that hasn't
stabilized. Adding eval infrastructure before observation is premature.

The reviewer's #1 (the "theater" charge) is partially correct but overstated —
there's a real difference between "I suspect date-boost is the right answer" and
"I've decided date-boost is the right answer regardless of data." Parking the plan
until there's actual usage data prevents that theater *and* prevents premature
commitment. Both sides of the objection close out in the same direction: wait.

---

## Trigger conditions to unpark

Resume this plan when:

1. **Real retrieval failure:** a query Renfield actually got asked in chat gives
   a wrong answer, and reproducing the query shows the retrieval layer is the
   cause (not the generator).
2. **Silent regression detected:** a mechanism (nightly canary, user feedback,
   anything) catches a retrieval-side regression that went unnoticed. This
   reinforces #5 from the external review and elevates observability work above
   the feature work.
3. **Use-case expansion:** someone other than the primary user starts using
   Renfield, surfacing multi-intent queries (→ query decomposition relevance)
   or image-document queries (→ VLM relevance).
4. **Two weeks post-worker-split stable:** if the worker-split proves stable and
   no #1-3 signals appear, that is itself the answer — current RAG is
   household-sufficient, and the comparison table was measuring the wrong thing
   for this scope.

---

## If unparking, start with observability not features

Per external review #5: before building any gap-closing feature, add a nightly
canary. Minimum viable version:

- Pick 3 query/expected-chunk-id pairs you care about
- Cron: run every night at 03:00 via a k8s CronJob
- Calls existing `POST /api/knowledge/rag-eval` with `mode=retrieval sample=3`
- Alert (email? push? discord?) if `hit@5` drops below baseline

This is smaller than the full plan, catches more failure modes than the full plan,
and ships in <1 day. If it catches something, you have a specific failure to
optimize for. If it doesn't, you save 2 weeks.

---

## References

- Office-hours design doc (personal archive):
  `~/.gstack/projects/ebongard-renfield/evdb-main-design-20260419-181300-rag-parity.md`
- Plan eng-review test plan (personal archive):
  `~/.gstack/projects/ebongard-renfield/evdb-main-eng-review-test-plan-20260419-181800.md`
- Existing eval infrastructure:
  - `src/backend/services/rag_eval_service.py`
  - `data/rag-eval/test_cases.yaml`
  - `POST /api/knowledge/rag-eval` admin endpoint
  - `tests/backend/test_rag_advanced.py:311-352`
- Comparison systems referenced:
  - RAG-Anything (16k★): github.com/HKUDS/RAG-Anything
  - LightRAG (34k★): github.com/HKUDS/LightRAG
  - RAGFlow (78k★): github.com/infiniflow/ragflow
  - LlamaIndex (49k★): github.com/run-llama/llama_index
  - GraphRAG (32k★): github.com/microsoft/graphrag
