# B.5 Operator Runbook

Single-doc walkthrough from "code is on the spike branch" to "decision artefact in `docs/B5_XTTS_EVAL.md`." Each phase has copy-paste commands, an estimated time, and the decision points that gate moving forward.

**Branch:** `spike/b5-xtts-eval`. **PR:** `#538`. **Plan:** [`docs/B5_PLAN.md`](B5_PLAN.md).

**Total elapsed time, end-to-end:** ~10.5 h. Maintenance window itself is ~50 min in the middle. Listening pass (Phase 5) is ~1.5-2 h that can run on a different day.

---

## Phase 1 — Build (operator action, ~30 min)

The spike Docker image must exist on Harbor before anything else can run.

### 1.1  Verify branch state

```bash
git fetch origin
git log origin/main..origin/spike/b5-xtts-eval --oneline
# Expect 7 commits: plan v3, Step 0 license, Step 1 Dockerfile,
# Step 2 dual-engine, Step 3 ref clip, Step 4 corpus, Step 5 benchmark.
```

### 1.2  Build + push on `.159`

The Renfield build box is `renfield.local` (192.168.1.159). It's the only host with the Harbor credentials cached and the bandwidth budget for ~3 GB image pushes.

```bash
ssh build@192.168.1.159
cd /opt/renfield
git fetch && git checkout spike/b5-xtts-eval
cd voice-server

# Build the spike image. Layer-1 pull from registry (the v0.1.5 base)
# uses ~1.5 GB; coqui-tts + GPU torch adds ~2.5 GB. Total build time
# ~10-15 min cold, ~2 min if base layers are cached on .159.
docker build -f Dockerfile.spike \
    -t registry.treehouse.x-idra.de/renfield/voice-server:b5-spike-rc1 .

# Push. ~3 GB over Harbor; allow 5-10 min.
docker push registry.treehouse.x-idra.de/renfield/voice-server:b5-spike-rc1
```

### 1.3  Build sanity-checks

If any of the following fails, surface BEFORE the maintenance window — debugging during the window blows the time budget.

- **`coqui-tts==0.27.0` install** — if pip resolves but the wheel rejects on Python 3.12, fall back to source: `pip install git+https://github.com/idiap/coqui-ai-TTS.git@v0.27.0`. ~30 min added.
- **GPU torch layer >2.5 GB** — if Harbor times out on push, edit `Dockerfile.spike` to split the torch install into two RUN lines (`nvidia-cudnn-cu12 nvidia-cublas-cu12` first, then `torch torchaudio`).
- **Smoke import** (optional, on .159 with NVIDIA runtime):
  ```bash
  docker run --rm --gpus all \
      registry.treehouse.x-idra.de/renfield/voice-server:b5-spike-rc1 \
      python -c "import torch; assert torch.cuda.is_available(); from TTS.api import TTS; print('OK')"
  ```

---

## Phase 2 — Pre-window prep (operator action, ~1 h)

Three artefacts need to exist on the cluster BEFORE the maintenance window. None of these touch production traffic.

### 2.1  Generate the thorsten reference clip + ConfigMap

The XTTS-v2 clone reference. Runs against the LIVE production voice-server pod (no downtime). 

**Storage:** the production NFS mount `/mnt/llm` is **read-only** by design (production should not mutate the model store). The clip is generated to a writable `/tmp/` path inside the pod, copied out via `kubectl cp`, and delivered to the spike pod via a `ConfigMap` (the WAV is ~470 KB, well under the 1 MB ConfigMap limit).

```bash
POD=$(kubectl --context renfield-private -n renfield get pod \
    -l app.kubernetes.io/name=voice-server \
    -o jsonpath='{.items[0].metadata.name}')

# Copy the script in
kubectl --context renfield-private cp \
    voice-server/scripts/generate_thorsten_ref.py \
    renfield/$POD:/tmp/

# Run it; XTTS_REF_OUTPUT redirects writes from the read-only /mnt/llm
# default to the pod-local /tmp.
kubectl --context renfield-private exec -n renfield $POD -- \
    env XTTS_REF_OUTPUT=/tmp/thorsten_ref.wav \
    python /tmp/generate_thorsten_ref.py
# Expect: ~470 KB WAV, 22.05 kHz mono 16-bit, ~10-14s duration.

# Pull out
mkdir -p /tmp/b5-prep
kubectl --context renfield-private cp \
    renfield/$POD:/tmp/thorsten_ref.wav \
    /tmp/b5-prep/thorsten_ref.wav

# Create ConfigMap (use `create` not `apply` — `apply`'s
# last-applied-configuration annotation hits the 256 KB limit on this
# binary-data ConfigMap).
kubectl --context renfield-private -n renfield create configmap xtts-thorsten-ref \
    --from-file=thorsten_ref.wav=/tmp/b5-prep/thorsten_ref.wav

# Verify
kubectl --context renfield-private -n renfield describe configmap xtts-thorsten-ref
```

The spike Deployment manifest (`k8s/voice-server-spike.yaml`) already mounts this ConfigMap at `/etc/xtts-ref/` and sets `XTTS_CLONE_VOICE_REF=/etc/xtts-ref/thorsten_ref.wav`. No further action needed at deploy time.

### 2.2  Extract production corpus sample

```bash
# Pull 30 candidate assistant messages from the last 7 days.
kubectl --context renfield-private exec -n renfield postgres-0 -- \
    psql -U renfield -d renfield -P pager=off -c "
    SELECT content
    FROM messages
    WHERE role = 'assistant'
      AND created_at > NOW() - INTERVAL '7 days'
      AND length(content) BETWEEN 20 AND 600
    ORDER BY random()
    LIMIT 30;" > /tmp/b5_candidates.txt

# Manually review /tmp/b5_candidates.txt:
#  - pick 10 representative prompts (3-4 short, 4-5 medium, 1-2 long)
#  - anonymise per voice-server/tests/b5/README.md table
#    (NAME / ORT / TEL / EMAIL / DATUM / PII)
#  - format as: prod-01: <text>
#  - save to: voice-server/tests/b5/corpus_production.txt (gitignored)
```

The corpus file lives on your local machine. It will be `kubectl cp`-ed into the spike pod during Phase 3.

### 2.3  Capture pre-window Piper baseline

The Step 6 in-window Piper-regression check needs a reference number from the live production v0.1.5 pod. ~5 min.

```bash
# 5-prompt smoke against production. Pick 5 prompts spanning the corpus
# categories (any 5 distinct lengths). Adjust the for-loop prompts.
POD=$(kubectl --context renfield-private -n renfield get pod \
    -l app.kubernetes.io/name=voice-server \
    -o jsonpath='{.items[0].metadata.name}')

for i in 1 2 3 4 5; do
    case $i in
        1) TEXT="Ja, gerne." ;;
        2) TEXT="Ich habe die Lampe im Wohnzimmer ausgeschaltet." ;;
        3) TEXT="Die Wäsche ist fertig. Soll ich sie in den Trockner geben?" ;;
        4) TEXT="Heute wird es vormittags überwiegend bewölkt mit Temperaturen um achtzehn Grad. Am Nachmittag kommt vereinzelt etwas Sonne durch." ;;
        5) TEXT="Der Termin ist am dreiundzwanzigsten Mai um neun Uhr fünfundvierzig." ;;
    esac
    echo -n "prompt $i: "
    kubectl --context renfield-private exec -n renfield $POD -- \
        sh -c "curl -s -o /dev/null -w 'HTTP=%{http_code} t=%{time_total}s\n' \
            -X POST http://localhost:8080/api/voice/tts \
            -H 'Content-Type: application/json' \
            -d '{\"text\":\"$TEXT\",\"engine\":\"piper\",\"language\":\"de\"}'"
done
```

Record the 5 timings in a notebook. Anything outside ±10 % of these in the in-window spike-Piper check means the spike image regressed Piper.

---

## Phase 3 — Maintenance window (operator action, ~50 min)

**Pick a household-quiet window before starting.** Voice unavailability is ~50 min.

### 3.1  Pre-flight (3 min)

```bash
# Confirm no active voice sessions in the last 60 s
kubectl --context renfield-private logs -n renfield deploy/voice-server --tail=20 \
    | grep -E "session_start|session_ready" || echo "no recent sessions"

# Note the satellite count for the post-window reconnect check
kubectl --context renfield-private get pods -A -l app=satellite -o wide || \
    echo "(no satellite resources — they connect from outside the cluster)"
```

### 3.2  Scale prod down + verify VRAM idle (5 min)

```bash
kubectl --context renfield-private scale -n renfield deploy/voice-server --replicas=0
kubectl --context renfield-private wait -n renfield --for=delete \
    pod -l app.kubernetes.io/name=voice-server --timeout=60s

# Wait 10s for CUDA cleanup, then probe VRAM. Expect <500 MB on a clean
# k8s-gpu-3 (just CUDA runtime baseline). >500 MB means a leftover
# allocation is still resident.
sleep 10
kubectl --context renfield-private debug node/k8s-gpu-3 \
    -it --image=nvidia/cuda:12.6.3-runtime-ubuntu24.04 -- \
    nvidia-smi --query-gpu=memory.used --format=csv,noheader,nounits
```

If VRAM stays >500 MB, investigate before deploying the spike — the contamination would skew the gate-#3 measurement.

### 3.3  Deploy spike + wait for ready (8 min)

```bash
kubectl --context renfield-private apply -f k8s/voice-server-spike.yaml
kubectl --context renfield-private wait -n renfield --for=condition=Ready \
    pod -l app.kubernetes.io/variant=spike-b5 --timeout=900s

SPIKE_POD=$(kubectl --context renfield-private -n renfield get pod \
    -l app.kubernetes.io/variant=spike-b5 \
    -o jsonpath='{.items[0].metadata.name}')
echo "spike pod: $SPIKE_POD"
```

### 3.4  Predownload XTTS-v2 + verify (5 min)

```bash
kubectl --context renfield-private exec -n renfield $SPIKE_POD -- \
    python /app/scripts/predownload_xtts.py
# Expect: ~3-5 min download, ~1.8 GB total, "File sizes:" log line.
```

### 3.5  Copy production corpus into pod (1 min)

```bash
kubectl --context renfield-private cp \
    voice-server/tests/b5/corpus_production.txt \
    renfield/$SPIKE_POD:/app/tests/b5/corpus_production.txt
```

### 3.6  Smoke gate — abort or proceed (3 min)

3 engines × 1 short prompt. If any fails, ROLL BACK before running the full benchmark.

```bash
for ENGINE in piper xtts-default xtts-clone; do
    REF_ARG=""
    if [ "$ENGINE" = "xtts-clone" ]; then
        REF_ARG=',"voice_ref":"/mnt/llm/voice/xtts_refs/thorsten_ref.wav"'
    fi
    echo -n "$ENGINE: "
    kubectl --context renfield-private exec -n renfield $SPIKE_POD -- \
        sh -c "curl -s -o /tmp/smoke_${ENGINE}.wav -w 'HTTP=%{http_code} bytes=%{size_download}\n' \
            -X POST http://localhost:8080/api/voice/tts \
            -H 'Content-Type: application/json' \
            -d '{\"text\":\"Heute scheint die Sonne über dem Garten.\",\"engine\":\"$ENGINE\",\"language\":\"de\"$REF_ARG}'"
done
```

**Pass criteria** (per engine):
- HTTP 200
- bytes >50 KB (a 1.5-second sentence at 22.05 kHz should be ~70 KB)
- Verify by playing back: `kubectl cp renfield/$SPIKE_POD:/tmp/smoke_xtts-clone.wav /tmp/ && open /tmp/smoke_xtts-clone.wav`

**If smoke fails:** capture logs, abort, jump to 3.10 rollback.

```bash
kubectl --context renfield-private logs -n renfield $SPIKE_POD --tail=200 > /tmp/spike_smoke_failure.log
```

### 3.7  Piper-regression check (3 min)

Same 5 prompts as Phase 2.3, but against the spike pod's Piper path.

```bash
for i in 1 2 3 4 5; do
    # ... same prompt cases as 2.3 ...
    kubectl --context renfield-private exec -n renfield $SPIKE_POD -- \
        sh -c "curl -s -o /dev/null -w 't=%{time_total}s\n' \
            -X POST http://localhost:8080/api/voice/tts \
            -H 'Content-Type: application/json' \
            -d '{\"text\":\"$TEXT\",\"engine\":\"piper\",\"language\":\"de\"}'"
done
```

**Pass criteria:** each timing within ±10 % of the Phase 2.3 baseline. If spike-Piper is consistently slower, the GPU-torch swap regressed Piper's CUDA EP — the comparison is biased toward XTTS. Decision:
- (a) Rebuild image with corrected dep pinning (~30 min — pushes the window over budget; consider rescheduling).
- (b) Continue but flag in the report that Piper numbers will use the Phase 2.3 baseline, not in-window measurements.

### 3.8  Run the full benchmark (15-25 min)

```bash
kubectl --context renfield-private exec -n renfield $SPIKE_POD -- \
    python /app/scripts/b5_benchmark.py \
        --output-dir /tmp/b5_results \
        --voice-ref /mnt/llm/voice/xtts_refs/thorsten_ref.wav \
        2>&1 | tee /tmp/b5_benchmark_log.txt
```

The harness logs progress per `(prompt, engine)` pair, prints a summary, and exits with code 0 (success) or 2 (failure-rate gate exceeded). If exit-2: see `/tmp/b5_results/_failures.csv` in the pod, decide whether to continue or abort.

### 3.9  Copy results out (2 min)

```bash
mkdir -p /tmp/b5_local
kubectl --context renfield-private cp \
    renfield/$SPIKE_POD:/tmp/b5_results /tmp/b5_local/

# Verify
ls /tmp/b5_local/b5_results/
# Expect: b5_results.json, b5_results.csv, _warmup.csv, _failures.csv, wavs/
ls /tmp/b5_local/b5_results/wavs/ | wc -l
# Expect: ~75 (25 prompts × 3 engines), or ~105 (with production corpus)
```

### 3.10  Tear down + scale prod back up (5 min)

```bash
kubectl --context renfield-private delete -f k8s/voice-server-spike.yaml
kubectl --context renfield-private wait -n renfield --for=delete \
    pod -l app.kubernetes.io/variant=spike-b5 --timeout=60s

# Restore production
kubectl --context renfield-private scale -n renfield deploy/voice-server --replicas=1
kubectl --context renfield-private wait -n renfield --for=condition=Ready \
    pod -l app.kubernetes.io/name=voice-server --timeout=600s
```

### 3.11  MANDATORY post-deploy E2E (5 min)

Per `memory/feedback_post_deploy_browser_e2e.md` — curl smoke alone does NOT validate the production rollback.

1. Open `https://renfield.local` in Chrome
2. Authenticate normally (whatever auth flow the household uses)
3. Ask one voice question (push-to-talk)
4. Verify TTS playback completes
5. Open DevTools → Network: confirm the WSS connection is `/ws/voice` and not `ws://localhost:...` (mixed-content regression)

If browser E2E fails: roll forward (don't try to roll back to v0.1.5 — already at v0.1.5). Investigate logs:
```bash
kubectl --context renfield-private logs -n renfield deploy/voice-server --tail=200
```

### 3.12  Satellite reconnect verification (5 min)

```bash
# Wait 60 s for satellites to auto-reconnect after voice-server is back.
sleep 60
kubectl --context renfield-private logs -n renfield deploy/voice-server --tail=100 \
    | grep -E "session_start|register" \
    | sort -u
```

Compare to the satellite roster (Phase 3.1 inventory). Per `memory/CRITICAL: Satellite Deployment Safety` — if any satellite is missing, do NOT remote-restart on the spot. Note as "missing, awaiting next satellite-deploy window" in the report.

---

## Phase 4 — Listening pass setup (~30 min)

This phase generates the blind A/B listening UI from the WAVs collected in Phase 3.9.

### 4.1  Generate listening HTML (TBD — Step 7 deliverable)

**Not yet implemented.** Step 7's `generate_listen_html.py` produces `b5_listen.html` from `/tmp/b5_local/b5_results/`. To implement when reaching Step 7.

The page:
- Randomly permutes the 3 engines per prompt, labels them A/B/C
- Hides the engine→label mapping in a separate JSON
- One prompt at a time, 1-5 score per slot
- Drift yes/no on long prompts only
- Saves to `localStorage`; "reveal mapping" button at end
- Two-pass: re-shuffle and score again after a 5-min break

---

## Phase 5 — Listening pass + report (~1.5-2 h listening + ~30 min writing)

### 5.1  Score all prompts (1.5-2 h)

Open `b5_listen.html` in a browser. Score 35 prompts × 3 engines × 2 passes = 210 ratings, with mandatory breaks after prompts 12 and 24 in each pass.

### 5.2  Aggregate + apply 4-gate threshold

Per Step 7's pre-committed thresholds:

1. MOS: XTTS-clone − Piper ≥ 0.5 on medium prompts
2. Latency: XTTS-clone p95 first-chunk TTFB ≤ 2 × Piper p95 TTFB
3. VRAM: XTTS-clone post-cache ≤ 8 GB; AND unloaded probe (if captured) ≤ 7 GB
4. Drift: XTTS-clone yes-count on long prompts ≤ 1 of 5

ANY gate fails → recommendation is "stay on Piper."

### 5.3  Write `docs/B5_XTTS_EVAL.md`

Methodology + results tables + 4-gate evaluation + decision. Append 1-paragraph summary to `docs/VOICE_PIPELINE_DESIGN.md` under the existing B.5 line.

### 5.4  Step 8 decision branch

Three pre-planned outcomes per `docs/B5_PLAN.md` Step 8. The license context (`docs/B5_LICENSE_NOTE.md` exit (a)) means even an XTTS win does NOT auto-promote — the swap is gated on a separate Reva-side license discussion and likely ships as opt-in only.

---

## Quick rollback reference

If anything in Phase 3 goes sideways and you need production back NOW:

```bash
kubectl --context renfield-private delete -f k8s/voice-server-spike.yaml --ignore-not-found
kubectl --context renfield-private scale -n renfield deploy/voice-server --replicas=1
# Wait for ready, then run the Phase 3.11 browser E2E
```

The production manifest (`k8s/voice-server.yaml`) is untouched throughout the spike. There is no scenario where production state is lost — the worst case is ~5 min of voice-server downtime while the rollback runs.
