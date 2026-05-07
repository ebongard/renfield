# B.5 License Note — XTTS-v2 / Coqui Public Model License 1.0.0

**Status:** Step 0 deliverable. **Exit (a) chosen 2026-05-07** — run the spike, narrow the swap-in's scope on win.
**Date:** 2026-05-07.

**Decision recorded:** spike proceeds with XTTS-v2 under CPML's testing/evaluation clause (which permits even commercial entities to do this). If XTTS wins on the 4-gate threshold, the swap-in PR does NOT auto-promote XTTS to default — instead, it documents XTTS as a per-deployment opt-in and records the German MOS / latency / VRAM numbers as the calibration bar for the next open-licensed candidate. Step 7's decision rule is amended in the plan accordingly. The harness, corpus, and listening UI built in Steps 1-7 remain reusable for any future TTS evaluation regardless of XTTS's outcome.
**Source recovered from:** `https://huggingface.co/coqui/XTTS-v2/raw/main/LICENSE.txt` (4 014 bytes, HTTP 200, retrieved 2026-05-07).

The canonical URL `coqui.ai/cpml` returns 404 (Coqui company shut down end-2023). The HuggingFace repo still ships the LICENSE.txt alongside the model weights — pinned and reproducible. CDX shows `web.archive.org` snapshots from 2023-10-21 through 2024-01-05 (5+ captures, all 200 OK) as a secondary cross-check if ever needed.

---

## Verbatim CPML clauses relevant to our use case

### Header

> Coqui Public Model License 1.0.0
> This license allows only non-commercial use of a machine learning model and its outputs.

### Grant

> The licensor grants you a copyright license to do everything you might do with the model that would otherwise infringe the licensor's copyright in it, **for any non-commercial purpose.** The licensor grants you a patent license that covers patent claims the licensor can license, or becomes able to license, that you would infringe by using the model in the form provided by the licensor, for any non-commercial purpose.

### Non-commercial Purpose definition

> Non-commercial purposes include any of the following uses of the model or its output, **but only so far as you do not receive any direct or indirect payment arising from the use of the model or its output.**
>
> - Personal use for research, experiment, and testing for the benefit of public knowledge, personal study, private entertainment, hobby projects, amateur pursuits, or religious observance.
> - **Use by commercial or for-profit entities for testing, evaluation, or non-commercial research and development.** Use of the model to train other models for commercial use is not a non-commercial purpose.
> - Use by any charitable organization for charitable purposes, or for testing or evaluation. Use for revenue-generating activity, including projects directly funded by government grants, is not a non-commercial purpose.

### No Other Rights

> These terms do not allow you to sublicense or transfer any of your licenses to anyone else, or prevent the licensor from granting licenses to anyone else. These terms do not imply any other licenses.

### Violations

> The first time you are notified in writing that you have violated any of these terms ... your licenses can nonetheless continue if you come into full compliance with these terms, and take practical steps to correct past violations, within 30 days of receiving notice. Otherwise, all your licenses end immediately.

---

## Application to our use cases

### Renfield (household self-hosted)

Renfield runs as a personal/household assistant. No direct or indirect payment from anyone for using the model. Maps onto the CPML clause:

> *Personal use for research, experiment, and testing ... personal study, private entertainment, hobby projects, amateur pursuits ...*

**Verdict for Renfield-only:** clearly permitted. The spike itself (Steps 1-7) is also permitted under both the personal-use clause and the explicit "Use by commercial or for-profit entities for testing, evaluation" clause, regardless of who runs it.

### Reva (commercial Enterprise Teams-Bot, per `memory/project_reva_compatibility.md`)

Reva is a commercial product. The "for-profit entity" clause applies, and within that clause:

> Use by commercial or for-profit entities **for testing, evaluation, or non-commercial research and development.**

**Reva can: evaluate, benchmark, develop against XTTS-v2.**
**Reva cannot: ship XTTS-v2 in any production deployment that generates revenue.**

This is unambiguous — the CPML's commercial-entity branch is restricted to non-production use. Production inference for paying customers (or where the bot itself is part of a paid product) is not permitted under any branch.

Compounding factor: **Coqui is defunct.** Even if Reva wanted to pay for a commercial license, there is no party available to grant one. The CPML's "license stewardship" sits with an organisation that no longer exists. The "evaluation only" branch is therefore a permanent ceiling, not a stepping stone.

### Cross-project framework problem

The plan asks: *if XTTS wins on MOS, swap Renfield's TTS engine to XTTS.* Per `memory/project_reva_compatibility.md`, Reva consumes Renfield as a framework. If Renfield's default TTS becomes XTTS, Reva inherits XTTS as the production engine — which the CPML forbids for Reva's commercial deployment.

Three resolutions exist:

1. **Renfield ships XTTS as default; Reva overrides via config to use Piper before commercial deploy.** Permitted by CPML (Reva's local override is not "use of the XTTS model" — it doesn't load the weights). But adds a config-must-be-changed-before-prod constraint. Easy to forget; security-relevant misconfig.
2. **Renfield ships XTTS as opt-in (default still Piper); operators self-select.** No automatic inheritance. Renfield documents the CPML constraint. Reva's default stays Piper; if Reva ever wants XTTS for non-commercial evaluation, operator opts in explicitly.
3. **Renfield does not ship XTTS at all.** Stays on Piper or pivots to an open-licensed alternative.

---

## Three plan exits, re-evaluated against the CPML text

### Exit (a) — Cleared for evaluation only

> Continue Steps 1-7. Decision artefact is produced. The swap-in PR is gated on a separate license review with the Reva owner.

**CPML status:** clearly permitted. Both Renfield (personal-use) and Reva (commercial-entity testing/evaluation) can run this spike.

**Implication:** if XTTS wins the spike, the swap-in PR cannot proceed under any production-default deployment without one of the three resolutions above. The most likely outcome is resolution #2 (opt-in only) or resolution #3 (don't ship at all).

The spike's *primary* value (deciding if XTTS is good enough for production) is therefore reduced to: "is XTTS good enough for the operators who explicitly opt in despite the CPML constraint?" — a much smaller question.

The spike's *secondary* value is real: the harness, corpus, methodology, and listening UI become reusable for the next TTS candidate evaluation. This is non-trivial.

### Exit (b) — Cleared for production (default engine)

**CPML status: NOT permitted** for the framework-distribution case. Renfield-as-household-only would be fine, but Renfield-as-Reva-framework would breach the CPML when Reva enters commercial production. Coqui is defunct so the breach is unresolvable.

This exit is closed.

### Exit (c) — Pivot to an open-licensed alternative

> Spike pivots to a candidate with no commercial-use restrictions. Harness, corpus, listening methodology stay; engine swaps.

Not chosen 2026-05-07 (exit (a) selected instead). Section retained as a reference for any future post-XTTS evaluation — these are the candidates the next round considers.

#### Verified candidate licenses (2026-05-07)

Code-license and model-weights-license are tracked separately. The CPML problem with XTTS-v2 is precisely a model-weights restriction with permissive code; F5-TTS replicates the same pattern. Code-only verification is insufficient.

| Candidate | Code (verified) | Model weights (verified) | German | Voice cloning | Net status |
|---|---|---|---|---|---|
| **ChatterboxTTS** (Resemble AI) | MIT (`github.com/resemble-ai/chatterbox` LICENSE) | **MIT** (HF `ResembleAI/chatterbox` cardData) | claimed in README languages table | yes | **Cleared.** Best XTTS replacement candidate. German *quality* unverified — needs its own listening pass before adoption. |
| **GPT-SoVITS** (RVC-Boss) | MIT (`github.com/RVC-Boss/GPT-SoVITS` LICENSE) | MIT (HF `lj1995/GPT-SoVITS` v1 cardData; v2 not under same HF id, license needs separate check if v2 is the target) | yes | yes (≤30 s reference) | **Cleared (v1).** Predominantly Chinese community — German pronunciation quality is anecdotal, would need benchmarking. |
| **Sherpa-onnx VITS-de** (k2-fsa runtime) | Apache 2.0 (`github.com/k2-fsa/sherpa-onnx` LICENSE) | **HF wrappers carry no declared license** (`csukuangfj/vits-piper-de_DE-thorsten-medium`, `csukuangfj/sherpa-onnx-vits-de` both return `license: None` from the API). The upstream Piper model IS MIT (`rhasspy/piper-voices` cardData). | yes (German-only model) | no (single voice per model) | **Effectively redundant.** The actual TTS quality comes from the upstream Piper model — sherpa-onnx is a faster runtime. Adopting this means "running Piper through a different runtime," NOT a quality upgrade. Drop unless the runtime-speed improvement matters independently. |
| **F5-TTS** (SWivid) | MIT (`github.com/SWivid/F5-TTS` LICENSE) | **CC-BY-NC-4.0** (HF `SWivid/F5-TTS` cardData) | yes | yes | **Eliminated — same shape as XTTS-v2.** Permissive code, non-commercial-only weights, with no party available to grant a commercial license. Do not consider for production deployment under our framework constraint. |

Recommendation if exit (c) ever fires: **ChatterboxTTS** is the primary candidate (only post-XTTS option with both MIT code AND MIT weights AND multilingual including German). Sherpa-onnx is not an upgrade over current Piper. F5-TTS and XTTS-v2 share the same disqualifier. GPT-SoVITS is fallback if ChatterboxTTS's German quality disappoints in benchmarking.

#### Pattern to remember for next license check

Code-license ≠ model-license. HuggingFace `cardData.license` is the authoritative source for model weights. GitHub `LICENSE` only governs the source code, which is often deliberately permissive even for non-commercial models. The XTTS-v2 / F5-TTS pair makes this distinction concrete: both have permissive code but restrict the weights — and the weights are what the spike actually loads.

---

## Recommendation

**Exit (a)** — run the spike, accept the production-shipping constraint will narrow the swap-in's scope.

Reasoning:
- CPML cost of the spike itself is zero (testing/evaluation is explicitly permitted).
- The harness + corpus + listening UI built in Steps 1-7 are reusable for any future TTS candidate; the cost is recovered even if XTTS ultimately can't ship.
- A measured XTTS-v2 result (German MOS, latency, VRAM on RTX 4060 Ti) is itself useful data for the broader open-source TTS landscape audit. We currently have no in-house numbers for this model on this hardware.
- Exit (c) right now would commit us to a candidate without first knowing what target quality we're trying to match. XTTS-v2 is the "high water mark" for German cloned-voice TTS as of 2026 per popular benchmarks; running it first sets the bar.

If exit (a) is chosen, the plan needs one explicit amendment: **Step 7's decision rule changes from "swap on win" to "swap-or-document-as-future-target on win"**, with the swap itself gated on a follow-up Reva license discussion.

---

## Decision (2026-05-07)

**Exit (a) — chosen.** Spike proceeds with XTTS-v2 under the CPML testing/evaluation clause.

**Step 7 amendment applied:** decision rule changed from "swap on win" to "swap-or-document-as-future-target on win" in `docs/B5_PLAN.md` (committed in the same revision as this note).

**ChatterboxTTS is the primary candidate** for any future post-XTTS evaluation — verified MIT code AND MIT weights AND multilingual support claimed. Quality benchmarking would happen in a future spike that reuses this spike's harness, corpus, and listening UI; only the engine adapter changes.
